"""Authoritative Coles half-price data via the public hotprices.org dump.

Why this exists
---------------
Scraping Coles ourselves means fighting Cloudflare/Imperva forever (the
``refresh_coles_specials`` "trickle" was slow, fragile, and needed a rested IP +
an awake machine). The open-source hotprices.org project (Javex/hotprices-au,
MIT) already scrapes Coles daily with a stealth browser and publishes a hosted,
gzipped JSON dump to a CDN. We consume *their* output — so we never touch Coles'
gate at all — and cache it into our own tables (source='hotprices') so we own
the history even if the project ever disappears.

What the dump gives us (verified):
  * ~21k Coles products with REAL Coles numeric ids (so the deterministic image
    CDN ``cdn.productimages.coles.com.au`` works for every product), and
  * per-product ``priceHistory`` — a newest-first list of price *change points*
    going back to ~2024.

The dump carries no explicit "was price" / "1/2 price" flag, so we DERIVE
half-price from the price series: a change-point is a half-price event when the
new price is <=~half the price it dropped from. That yields both this week's
currently-half list AND every historical half-price event per product — the
latter feeds the cycle predictor with real multi-year history.

Public surface:
    fetch_dump(retailer='coles', *, log) -> list[dict]
    parse_products(raw, *, log) -> list[ColesProduct]
    scrape(log) -> ScrapeOutput            # this week's currently-half specials
    build_coles_image_url(coles_id) -> str
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from src.models import ScrapeOutput, ScrapeRun, WeeklySpecial

# Per-retailer canonical dump URLs (CloudFront-hosted, public, daily refresh).
HOTPRICES_URLS = {
    "coles": "https://hotprices.org/data/latest-canonical.coles.compressed.json.gz",
    "woolworths": "https://hotprices.org/data/latest-canonical.woolies.compressed.json.gz",
}

# A change-point counts as a half-price event when the price dropped by at least
# this fraction vs the price it dropped FROM. Coles "1/2 Price" is exactly 50%;
# 0.48 tolerates odd ticket prices / rounding while excluding mere 30-40% sales.
_EVENT_MIN_OFF = 0.48
# A product is "currently half-price" if its current price is at least this far
# below its recent regular (max) price — a second signal that also catches a
# current half that followed straight after another promo.
_CURRENT_MIN_OFF = 0.48
# Window (days) for estimating the "regular" (ticket) price = max price seen.
_REGULAR_WINDOW_DAYS = 300

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WednesdayIngestion/1.0"


@dataclass
class HalfPriceEvent:
    on_date: date
    sale_cents: int
    regular_cents: int
    discount_pct: int


@dataclass
class ColesProduct:
    coles_id: str
    name: str
    image_url: str | None
    source_product_url: str | None
    regular_cents: int
    current_sale_cents: int | None
    current_discount_pct: int | None
    is_current_half: bool
    events: list[HalfPriceEvent] = field(default_factory=list)


def build_coles_image_url(coles_id: str) -> str:
    """Deterministic, ungated Coles product-image CDN URL."""
    cid = str(coles_id)
    return f"https://cdn.productimages.coles.com.au/productimages/{cid[0]}/{cid}.jpg"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _coles_product_url(coles_id: str, name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return f"https://www.coles.com.au/product/{slug}-{coles_id}"


def _cents(price) -> int | None:
    try:
        c = round(float(price) * 100)
    except (TypeError, ValueError):
        return None
    return c if c > 0 else None


def fetch_dump(retailer: str = "coles", *, log: logging.Logger) -> list[dict]:
    """Download + gunzip + parse the canonical dump for one retailer."""
    url = HOTPRICES_URLS[retailer]
    log.info("hotprices.fetch retailer=%s url=%s", retailer, url)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
    data = json.loads(gzip.decompress(raw))
    if not isinstance(data, list):
        raise ValueError(f"unexpected dump shape: {type(data).__name__}")
    log.info("hotprices.fetched retailer=%s bytes=%d products=%d", retailer, len(raw), len(data))
    return data


def _history_points(raw: dict) -> list[tuple[date, int]]:
    """Newest-first list of (date, price_cents) change points, well-formed only."""
    out: list[tuple[date, int]] = []
    for e in raw.get("priceHistory") or []:
        c = _cents(e.get("price"))
        d = e.get("date")
        if c is None or not d:
            continue
        try:
            out.append((date.fromisoformat(d), c))
        except (TypeError, ValueError):
            continue
    return out


def _half_price_events(hist: list[tuple[date, int]]) -> list[HalfPriceEvent]:
    """Detect half-price drops across the change-point series (newest-first)."""
    events: list[HalfPriceEvent] = []
    for i in range(len(hist) - 1):
        on_date, now_c = hist[i]
        was_c = hist[i + 1][1]
        if was_c > 0 and now_c > 0 and (1 - now_c / was_c) >= _EVENT_MIN_OFF:
            pct = round(100 * (1 - now_c / was_c))
            events.append(HalfPriceEvent(on_date, now_c, was_c, pct))
    return events


def _regular_cents(hist: list[tuple[date, int]], today: date) -> int:
    """Estimate the ticket (regular) price = max price within the recent window."""
    cutoff = today - timedelta(days=_REGULAR_WINDOW_DAYS)
    recent = [c for (d, c) in hist if d >= cutoff]
    pool = recent or [c for (_d, c) in hist]
    return max(pool) if pool else 0


def _parse_one(raw: dict, *, today: date) -> ColesProduct | None:
    cid = raw.get("id")
    name = (raw.get("name") or "").strip()
    if cid is None or not name:
        return None
    cid = str(cid)
    hist = _history_points(raw)
    if not hist:
        return None

    events = _half_price_events(hist)
    regular = _regular_cents(hist, today)
    current = hist[0][1]

    # Currently half-price if the newest change-point is itself a half-price drop,
    # OR the current price is >=~half below the recent regular (catches a current
    # promo that followed straight on from another).
    is_half = bool(events) and events[0].on_date == hist[0][0]
    if not is_half and regular > 0 and (1 - current / regular) >= _CURRENT_MIN_OFF:
        is_half = True

    cur_sale = current if is_half else None
    cur_pct = round(100 * (1 - current / regular)) if (is_half and regular > 0) else None

    return ColesProduct(
        coles_id=cid,
        name=name,
        image_url=build_coles_image_url(cid),
        source_product_url=_coles_product_url(cid, name),
        regular_cents=regular,
        current_sale_cents=cur_sale,
        current_discount_pct=cur_pct,
        is_current_half=is_half,
        events=events,
    )


def parse_products(raw_items: list[dict], *, log: logging.Logger,
                   today: date | None = None) -> list[ColesProduct]:
    """Parse every product; keep only those with usable price history."""
    today = today or datetime.now(timezone.utc).date()
    products: list[ColesProduct] = []
    for raw in raw_items:
        p = _parse_one(raw, today=today)
        if p is not None:
            products.append(p)
    half = sum(1 for p in products if p.is_current_half)
    with_hist = sum(1 for p in products if p.events)
    log.info("hotprices.parsed products=%d currently_half=%d ever_half=%d",
             len(products), half, with_hist)
    return products


def _most_recent_wednesday(today: date) -> date:
    return today - timedelta(days=(today.weekday() - 2) % 7)


def scrape(log: logging.Logger, *, today: date | None = None) -> ScrapeOutput:
    """This week's authoritative Coles half-price specials, derived from the dump."""
    run = ScrapeRun(
        source="hotprices",
        started_at=datetime.now(timezone.utc),
        source_url=HOTPRICES_URLS["coles"],
    )
    today = today or datetime.now(timezone.utc).date()
    week_start = _most_recent_wednesday(today)
    week_end = week_start + timedelta(days=6)
    scraped_at = datetime.now(timezone.utc)

    try:
        raw = fetch_dump("coles", log=log)
        products = parse_products(raw, log=log, today=today)
    except Exception as e:  # noqa: BLE001
        log.exception("hotprices.scrape_error")
        run.finalise(status="failed", items=0, error=str(e))
        return ScrapeOutput(run=run)

    specials: list[WeeklySpecial] = []
    for p in products:
        if not p.is_current_half or p.current_sale_cents is None:
            continue
        pct = p.current_discount_pct or 0
        specials.append(WeeklySpecial(
            retailer="coles",
            product_name=p.name,
            category="Uncategorised",
            regular_price_cents=p.regular_cents,
            sale_price_cents=p.current_sale_cents,
            discount_pct=pct,
            is_half_price=pct >= 48,
            last_halfprice_raw="",
            last_halfprice_weeks_ago=None,
            last_halfprice_retailer=None,
            week_start=week_start,
            week_end=week_end,
            source="hotprices",
            source_url=p.source_product_url or HOTPRICES_URLS["coles"],
            scraped_at=scraped_at,
            image_url=p.image_url,
        ))

    run.finalise(status="success" if specials else "no_data", items=len(specials))
    run.notes = f"currently_half={len(specials)} week_start={week_start}"
    log.info("hotprices.scrape_done specials=%d week_start=%s", len(specials), week_start)
    return ScrapeOutput(run=run, specials=specials)


if __name__ == "__main__":
    from src.scrapers.base import configure_logging

    _log = configure_logging(verbose=True)
    _raw = fetch_dump("coles", log=_log)
    _today = datetime.now(timezone.utc).date()
    _prods = parse_products(_raw, log=_log, today=_today)
    _cur = [p for p in _prods if p.is_current_half]
    _ge3 = [p for p in _prods if len(p.events) >= 3]
    _total_events = sum(len(p.events) for p in _prods)
    print()
    print(f"products parsed       = {len(_prods)}")
    print(f"currently half-price  = {len(_cur)}")
    print(f">=3 historical cycles = {len(_ge3)}")
    print(f"total event rows      = {_total_events}")
    print()
    for p in _cur[:8]:
        print(f"  -{p.current_discount_pct:>2}%  ${p.regular_cents/100:>6.2f} -> ${p.current_sale_cents/100:>6.2f}  "
              f"({len(p.events)} cycles)  {p.name[:44]}")
        print(f"        img={p.image_url}")
