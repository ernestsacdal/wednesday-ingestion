"""Authoritative Coles half-price specials via the website's embedded data.

The parallel of woolies_specials.py for Coles. StockUp proved unreliable for
both retailers; Woolworths got fixed via its browse API, and Coles is fixed
here.

Coles is a Next.js SPA, but its pages embed the fully-rendered page data in a
``<script id="__NEXT_DATA__">`` tag — and crucially the half-price specials
listing ``/on-special?filter_Special=halfprice`` returns 200 with that data to
a polite browser-headed GET (no Cloudflare challenge on single spaced
requests). ``props.pageProps.searchResults`` gives ``noOfResults`` (~1,140),
``pageSize`` (48) and a ``results[]`` array; each PRODUCT carries ``id`` (the
real numeric product id), ``name``, ``brand``, ``size``, ``imageUris`` and a
``pricing`` object (``now`` / ``was`` / ``savePercent`` / ``priceDescription``
"1/2 Price"). Image lives at the same deterministic CDN we already use.

Cloudflare DOES rate-challenge sustained bursts, so we page politely with a
delay + detect the HTML challenge (no __NEXT_DATA__ / no searchResults) and
back off; persistent challenge returns whatever we collected (partial is still
far better + more accurate than StockUp).

Public surface:
    scrape(session, log) -> ScrapeOutput
    build_coles_session() -> requests.Session
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.models import ScrapeOutput, ScrapeRun, WeeklySpecial

_HALF_PRICE_URL = "https://www.coles.com.au/on-special?filter_Special=halfprice"
_CDN_BASE = "https://cdn.productimages.coles.com.au/productimages"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_PAGE_SIZE = 48
_MAX_PAGES = 40  # safety ceiling; real count ~24 pages
# Coles rate-limits ~5-6 requests/window via Cloudflare, so pace gently and,
# on a challenge, back off long enough (no requests) for the token bucket to
# refill before resuming.
_DELAY_SECONDS = 5.0
_CHALLENGE_BACKOFFS_S = (60, 120, 240)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="120", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


class ColesChallenged(RuntimeError):
    """Raised when Cloudflare persistently challenges the listing page."""


def build_coles_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    return s


def _fetch_search_results(session: requests.Session, page: int, log: logging.Logger) -> dict[str, Any]:
    """GET one half-price listing page, return searchResults dict.

    Retries through Cloudflare HTML challenges. Raises ColesChallenged on
    persistent failure.
    """
    url = f"{_HALF_PRICE_URL}&page={page}"
    for wait in (0, *_CHALLENGE_BACKOFFS_S):
        if wait:
            log.info("coles_specials.challenge_backoff seconds=%d page=%d", wait, page)
            time.sleep(wait)
        resp = session.get(url, timeout=25)
        m = _NEXT_DATA_RE.search(resp.text)
        if resp.status_code == 200 and m:
            try:
                data = json.loads(m.group(1))
                sr = data.get("props", {}).get("pageProps", {}).get("searchResults")
                if isinstance(sr, dict):
                    return sr
            except (ValueError, AttributeError):
                pass
    raise ColesChallenged(f"persistent Cloudflare challenge for {url}")


def fetch_search_results_once(session: requests.Session, page: int) -> dict[str, Any] | None:
    """Single GET of one half-price page. Returns the searchResults dict, or
    None when Cloudflare challenges (no __NEXT_DATA__). No internal retry — the
    trickle runner owns the long-quiet backoff so it never pokes during the
    rate-limit cooldown."""
    url = f"{_HALF_PRICE_URL}&page={page}"
    resp = session.get(url, timeout=25)
    m = _NEXT_DATA_RE.search(resp.text)
    if resp.status_code == 200 and m:
        try:
            data = json.loads(m.group(1))
            sr = data.get("props", {}).get("pageProps", {}).get("searchResults")
            if isinstance(sr, dict):
                return sr
        except (ValueError, AttributeError):
            pass
    return None


def to_special(p: dict[str, Any], *, week_start, week_end, scraped_at) -> WeeklySpecial | None:
    """Public wrapper around the row mapper for the trickle runner."""
    return _to_special(p, week_start=week_start, week_end=week_end, scraped_at=scraped_at)


def _to_special(p: dict[str, Any], *, week_start, week_end, scraped_at) -> WeeklySpecial | None:
    if p.get("_type") != "PRODUCT":
        return None
    pid = p.get("id")
    pricing = p.get("pricing") or {}
    now = pricing.get("now")
    was = pricing.get("was")
    name = (p.get("name") or "").strip()
    if pid is None or now is None or was is None or not name:
        return None
    try:
        sale_cents = round(float(now) * 100)
        reg_cents = round(float(was) * 100)
    except (TypeError, ValueError):
        return None
    if reg_cents <= 0 or sale_cents <= 0 or sale_cents >= reg_cents:
        return None

    brand = (p.get("brand") or "").strip()
    full_name = f"{brand} {name}".strip() if brand and not name.lower().startswith(brand.lower()) else name
    discount_pct = pricing.get("savePercent")
    if not isinstance(discount_pct, int):
        discount_pct = round(100 * (reg_cents - sale_cents) / reg_cents)

    # Image: prefer the API's imageUris path on the Coles CDN; the asset id == product id.
    image_url = None
    uris = p.get("imageUris") or []
    if uris and isinstance(uris, list) and uris[0].get("uri"):
        image_url = f"{_CDN_BASE}{uris[0]['uri']}"
    else:
        sid = str(pid)
        image_url = f"{_CDN_BASE}/{sid[0]}/{sid}.jpg"

    return WeeklySpecial(
        retailer="coles",
        product_name=full_name,
        category="Uncategorised",
        regular_price_cents=reg_cents,
        sale_price_cents=sale_cents,
        discount_pct=discount_pct,
        is_half_price=discount_pct >= 50,
        last_halfprice_raw="",
        last_halfprice_weeks_ago=None,
        last_halfprice_retailer=None,
        week_start=week_start,
        week_end=week_end,
        source="coles_catalogue",
        source_url=_HALF_PRICE_URL,
        scraped_at=scraped_at,
        image_url=image_url,
    )


def _most_recent_wednesday():
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=(today.weekday() - 2) % 7)


def scrape(session: requests.Session, log: logging.Logger) -> ScrapeOutput:
    run = ScrapeRun(
        source="coles_catalogue",
        started_at=datetime.now(timezone.utc),
        source_url=_HALF_PRICE_URL,
    )
    week_start = _most_recent_wednesday()
    week_end = week_start + timedelta(days=6)
    scraped_at = datetime.now(timezone.utc)

    specials: list[WeeklySpecial] = []
    seen_ids: set[Any] = set()
    total_expected: int | None = None

    try:
        for page in range(1, _MAX_PAGES + 1):
            sr = _fetch_search_results(session, page, log)
            if total_expected is None:
                total_expected = sr.get("noOfResults")
                log.info("coles_specials.total expected=%s", total_expected)
            results = sr.get("results") or []
            new_this_page = 0
            for p in results:
                if p.get("_type") != "PRODUCT":
                    continue
                if p.get("id") in seen_ids:
                    continue
                sp = _to_special(p, week_start=week_start, week_end=week_end, scraped_at=scraped_at)
                if sp is None:
                    continue
                seen_ids.add(p.get("id"))
                specials.append(sp)
                new_this_page += 1
            log.info("coles_specials.page page=%d collected=%d", page, len(specials))
            # Stop when the page yielded no new products, or we've covered the total.
            if new_this_page == 0:
                break
            if total_expected is not None and len(seen_ids) >= total_expected:
                break
            time.sleep(_DELAY_SECONDS)
    except ColesChallenged as e:
        if specials:
            run.finalise(status="partial", items=len(specials), error=str(e))
            run.notes = f"partial; expected={total_expected}"
            log.warning("coles_specials.partial collected=%d expected=%s", len(specials), total_expected)
            return ScrapeOutput(run=run, specials=specials)
        run.finalise(status="failed", items=0, error=str(e))
        return ScrapeOutput(run=run)
    except Exception as e:  # noqa: BLE001
        log.exception("coles_specials.error")
        run.finalise(status="partial" if specials else "failed", items=len(specials), error=str(e))
        return ScrapeOutput(run=run, specials=specials)

    half = sum(1 for s in specials if s.is_half_price)
    run.finalise(status="success" if specials else "no_data", items=len(specials))
    run.notes = f"half_price={half} expected={total_expected}"
    log.info("coles_specials.done collected=%d half_price=%d expected=%s",
             len(specials), half, total_expected)
    return ScrapeOutput(run=run, specials=specials)


if __name__ == "__main__":
    from src.scrapers.base import configure_logging

    log = configure_logging(verbose=True)
    out = scrape(build_coles_session(), log)
    print()
    print(f"status     = {out.run.status}")
    print(f"specials   = {len(out.specials)}")
    print(f"half_price = {sum(1 for s in out.specials if s.is_half_price)}")
    print(f"notes      = {out.run.notes}")
    print()
    for s in out.specials[:8]:
        print(f"  {s.discount_pct:>3}%  ${s.regular_price_cents/100:>6.2f} -> ${s.sale_price_cents/100:>6.2f}  {s.product_name[:48]}")
