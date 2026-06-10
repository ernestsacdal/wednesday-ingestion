"""Authoritative Woolworths half-price specials via the official browse API.

Why this exists
---------------
The StockUp Google Sheet (src/scrapers/stockup_sheet.py) turned out to be a
price-*tracking* dataset, not "this week's specials": spot-checks against the
live Woolworths API showed ~half its "half-price" rows were either a smaller
discount or not on special at all. So for Woolworths we stop trusting StockUp
and read the retailer's own data.

Woolworths exposes its specials category tree at
``/apis/ui/PiesCategoriesWithSpecials`` and the "Half Price" node is
``specialsgroup.3676``. Its products are enumerable (with real Price /
WasPrice / IsOnSpecial / Stockcode / image) via the browse API
``/apis/ui/browse/category``. We paginate that node to get the complete,
accurate half-price list (~1,700 items), no scraping/Cloudflare involved.

Public surface:
    scrape(session, log) -> ScrapeOutput
    build_woolies_session() -> requests.Session

Coles has no equivalent open endpoint yet; it stays on StockUp until a
reliable Coles price source is found (tracked separately).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.models import ScrapeOutput, ScrapeRun, WeeklySpecial
from src.scrapers.product_images import _BROWSER_HEADERS, build_image_session

_BROWSE_API = "https://www.woolworths.com.au/apis/ui/browse/category"
_HALF_PRICE_NODE = "specialsgroup.3676"
_HALF_PRICE_URL = "/shop/browse/specials/half-price"
_PAGE_SIZE = 36  # the browse API rejects larger page sizes with HTTP 400
_MAX_PAGES = 80  # safety ceiling; real count is ~1,700 (~47 pages)
_DELAY_SECONDS = 0.4


def build_woolies_session() -> requests.Session:
    # Same primed, browser-headed session the image lookup uses.
    return build_image_session()


def _browse_page(session: requests.Session, page: int) -> dict[str, Any]:
    body = {
        "categoryId": _HALF_PRICE_NODE,
        "pageNumber": page,
        "pageSize": _PAGE_SIZE,
        "sortType": "TraderRelevance",
        "url": _HALF_PRICE_URL,
        "location": _HALF_PRICE_URL,
        "formatObject": '{"name":"Half Price"}',
        "isSpecial": True,
        "isBundle": False,
        "isMobile": False,
        "filters": [],
        "token": "",
        "gpBoost": 0,
        "isHideUnavailableProducts": False,
        "enableAdReRanking": False,
        "groupEdmVariants": True,
        "categoryVersion": "v2",
    }
    headers = {**_BROWSER_HEADERS, "Referer": f"https://www.woolworths.com.au{_HALF_PRICE_URL}"}
    resp = session.post(_BROWSE_API, json=body, headers=headers, timeout=25)
    if resp.status_code != 200:
        raise ValueError(f"http_{resp.status_code}")
    return resp.json()


def _iter_products(page_json: dict[str, Any]):
    """Flatten Bundles[].Products[] -> individual product dicts."""
    for bundle in page_json.get("Bundles") or []:
        for p in bundle.get("Products") or []:
            if isinstance(p, dict):
                yield p


def _to_special(
    p: dict[str, Any], *, week_start, week_end, scraped_at,
) -> WeeklySpecial | None:
    name = (p.get("Name") or "").strip()
    price = p.get("Price")
    was = p.get("WasPrice")
    if not name or price is None or was is None:
        return None
    try:
        sale_cents = round(float(price) * 100)
        reg_cents = round(float(was) * 100)
    except (TypeError, ValueError):
        return None
    # Only genuine discounts (the category should guarantee this, but be safe).
    if reg_cents <= 0 or sale_cents <= 0 or sale_cents >= reg_cents:
        return None
    discount_pct = round(100 * (reg_cents - sale_cents) / reg_cents)
    image_url = (
        p.get("LargeImageFile") or p.get("MediumImageFile") or p.get("SmallImageFile") or None
    )
    stockcode = p.get("Stockcode")
    return WeeklySpecial(
        retailer="woolworths",
        product_name=name,
        category=(p.get("Department") or "Uncategorised").strip() or "Uncategorised",
        regular_price_cents=reg_cents,
        sale_price_cents=sale_cents,
        discount_pct=discount_pct,
        is_half_price=discount_pct >= 50,
        last_halfprice_raw="",
        last_halfprice_weeks_ago=None,
        last_halfprice_retailer=None,
        week_start=week_start,
        week_end=week_end,
        source="woolies_catalogue",
        source_url=f"https://www.woolworths.com.au{_HALF_PRICE_URL}",
        scraped_at=scraped_at,
        image_url=image_url,
        # Real Woolies key (matches the hotprices Woolies dump id = stockcode),
        # so the live-API and dump-fallback paths upsert the same product row.
        retailer_sku=f"woolworths:{stockcode}" if stockcode is not None else None,
    )


def _most_recent_wednesday():
    today = datetime.now(timezone.utc).date()
    # Monday=0 ... Wednesday=2
    return today - timedelta(days=(today.weekday() - 2) % 7)


def scrape(session: requests.Session, log: logging.Logger) -> ScrapeOutput:
    run = ScrapeRun(
        source="woolies_catalogue",
        started_at=datetime.now(timezone.utc),
        source_url=f"https://www.woolworths.com.au{_HALF_PRICE_URL}",
    )
    week_start = _most_recent_wednesday()
    week_end = week_start + timedelta(days=6)
    scraped_at = datetime.now(timezone.utc)

    specials: list[WeeklySpecial] = []
    seen_names: set[str] = set()
    total_expected: int | None = None

    try:
        for page in range(1, _MAX_PAGES + 1):
            data = _browse_page(session, page)
            if total_expected is None:
                total_expected = data.get("TotalRecordCount")
                log.info("woolies_specials.total expected=%s", total_expected)
            page_products = list(_iter_products(data))
            if not page_products:
                break
            for p in page_products:
                sp = _to_special(p, week_start=week_start, week_end=week_end, scraped_at=scraped_at)
                if sp is None:
                    continue
                # Dedup by name (browse can repeat across grouped tiles).
                key = sp.product_name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                specials.append(sp)
            log.info("woolies_specials.page page=%d collected=%d", page, len(specials))
            if total_expected is not None and len(seen_names) >= total_expected:
                break
            time.sleep(_DELAY_SECONDS)
    except Exception as e:  # noqa: BLE001 — partial data is still useful
        log.exception("woolies_specials.error")
        if specials:
            run.finalise(status="partial", items=len(specials), error=str(e))
            return ScrapeOutput(run=run, specials=specials)
        run.finalise(status="failed", items=0, error=str(e))
        return ScrapeOutput(run=run)

    half = sum(1 for s in specials if s.is_half_price)
    run.finalise(status="success" if specials else "no_data", items=len(specials))
    run.notes = f"half_price={half} expected={total_expected}"
    log.info("woolies_specials.done collected=%d half_price=%d expected=%s",
             len(specials), half, total_expected)
    return ScrapeOutput(run=run, specials=specials)


if __name__ == "__main__":
    from src.scrapers.base import configure_logging

    log = configure_logging(verbose=True)
    out = scrape(build_woolies_session(), log)
    print()
    print(f"status        = {out.run.status}")
    print(f"specials      = {len(out.specials)}")
    print(f"half_price    = {sum(1 for s in out.specials if s.is_half_price)}")
    print(f"notes         = {out.run.notes}")
    print()
    for s in out.specials[:8]:
        print(f"  {s.discount_pct:>3}%  ${s.regular_price_cents/100:>6.2f} -> ${s.sale_price_cents/100:>6.2f}  {s.product_name[:50]}")
