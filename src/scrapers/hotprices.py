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
from pathlib import Path

from src.models import ScrapeOutput, ScrapeRun, WeeklySpecial

# Category code -> human label, vendored from the hotprices-au project's
# web/site/model/categories.js (the dump carries only the numeric code).
_CATEGORY_LABELS: dict[str, str] = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "hotprices_categories.json")
    .read_text(encoding="utf-8")
)


# Marketplace / non-grocery exclusion (2026-06-12). ~73% of the Woolies dump
# is "Everyday Market" third-party marketplace stock (car mats, perfume,
# $999 pet strollers with fake was-prices) — not supermarket groceries. Two
# rules, validated against the live dump:
#   1. Real Woolworths supermarket stockcodes are <= 7 digits; marketplace
#      ids are 10 digits. The split is perfectly bimodal (nothing at 8-9).
#   2. A small category denylist for the unambiguous general-merchandise
#      groups (Everyday Market, Hampers & Gifting, the Home & Lifestyle
#      group). Garden/hardware/pet codes are NOT denied — real supermarket
#      items (potting mix, batteries, pet food) live there; rule 1 already
#      kills the marketplace stock inside them.
_MARKETPLACE_MIN_ID_LEN = 8
_NON_GROCERY_CODES = frozenset(
    {"778", "779", "10104", "10105", "11111"}
    | {str(c) for c in range(13125, 13144)}  # Home & Lifestyle group
)


def _is_marketplace(raw: dict, retailer: str) -> bool:
    """True for third-party marketplace / non-grocery dump items to skip."""
    if retailer == "woolworths" and len(str(raw.get("id") or "")) >= _MARKETPLACE_MIN_ID_LEN:
        return True
    return str(raw.get("category")) in _NON_GROCERY_CODES


# Conservative name-keyword fallback for the ~30% of dump items that carry
# no category code (notably most Coles chips/snacks). Rules are ordered —
# first hit wins — and deliberately narrow: a product that matches nothing
# stays honestly 'Uncategorised' rather than being guessed into a shelf.
_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Frozen first so "frozen chips" never lands in Savoury Snacks.
    ("Ice Cream & Frozen Desserts", ("ice cream", "gelato", "icy pole", "paddle pop")),
    ("Frozen", ("frozen",)),
    ("Savoury Snacks", (
        "potato chips", "corn chips", "crisps", "popcorn", "pretzel",
        "doritos", "pringles", "cheezels", "twisties", "burger rings",
        "grain waves", "rice crackers", "snack mix",
    )),
    ("Confectionery", (
        "chocolate", "lollies", "gummy", "gummi", "licorice", "liquorice",
        "candy", "marshmallow", "fudge", "chewing gum",
    )),
    ("Biscuits & Crackers", ("biscuit", "cookie", "cracker", "wafer")),
    ("Soft Drinks", ("soft drink", "cola", "lemonade")),
    ("Energy Drinks", ("energy drink",)),
    ("Juice", ("juice",)),
    ("Yogurt", ("yoghurt", "yogurt")),
    ("Cheese", ("cheese block", "cheese slices", "shredded cheese", "cheese grated")),
    ("Milk", ("milk 1l", "milk 2l", "milk 3l", "long life milk", "uht milk")),
    ("Laundry", ("laundry", "fabric softener", "stain remover")),
    ("Toilet Paper, Tissues & Paper Towels", ("toilet paper", "toilet tissue", "paper towel", "facial tissues")),
    ("Cleaning Goods", ("dishwash", "disinfectant", "bleach", "toilet cleaner", "multipurpose cleaner", "surface spray")),
]


def _keyword_category(name: str) -> str:
    lowered = name.lower()
    for label, needles in _KEYWORD_RULES:
        if any(n in lowered for n in needles):
            return label
    return "Uncategorised"


def category_label(code, name: str = "") -> str:
    """Human category for a dump code, keyword fallback when uncoded."""
    label = _CATEGORY_LABELS.get(str(code))
    if label is not None:
        return label
    return _keyword_category(name) if name else "Uncategorised"

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
# A product is "on special" (for the broader Specials view) at this shallower
# floor. SEPARATE from the half-price floors above: items >=30% but <48% off are
# emitted tagged is_half_price=False and NEVER feed the predictor (which only
# reads is_half_price=true rows). Changing this does not affect half-price.
_SPECIAL_MIN_OFF = 0.30
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
    category: str = "Uncategorised"
    events: list[HalfPriceEvent] = field(default_factory=list)


def build_coles_image_url(coles_id: str) -> str:
    """Deterministic, ungated Coles product-image CDN URL."""
    cid = str(coles_id)
    return f"https://cdn.productimages.coles.com.au/productimages/{cid[0]}/{cid}.jpg"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _coles_product_url(coles_id: str, name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return f"https://www.coles.com.au/product/{slug}-{coles_id}"


def build_woolies_image_url(stockcode: str) -> str:
    """Deterministic Woolworths product-image CDN URL (the dump id IS the stockcode)."""
    return f"https://cdn0.woolworths.media/content/wowproductimages/large/{stockcode}.jpg"


def _image_url(retailer: str, pid: str) -> str:
    return build_woolies_image_url(pid) if retailer == "woolworths" else build_coles_image_url(pid)


def _product_url(retailer: str, pid: str, name: str) -> str:
    if retailer == "woolworths":
        return f"https://www.woolworths.com.au/shop/productdetails/{pid}"
    return _coles_product_url(pid, name)


def _cents(price) -> int | None:
    try:
        c = round(float(price) * 100)
    except (TypeError, ValueError):
        return None
    return c if c > 0 else None


# A real dump is ~21k (Coles) / ~72k (Woolies) items. Anything below this is a
# truncated or corrupt file — fail loudly rather than write a near-empty week.
_MIN_DUMP_ITEMS = 10_000


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
    if len(data) < _MIN_DUMP_ITEMS:
        raise ValueError(
            f"suspiciously small {retailer} dump ({len(data)} items < {_MIN_DUMP_ITEMS}) — "
            "refusing to treat a truncated file as authoritative"
        )
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


def _parse_one(raw: dict, *, today: date, retailer: str = "coles") -> ColesProduct | None:
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
    # promo that followed straight on from another). UNCHANGED — this is the flag
    # the predictor + half-price set depend on.
    is_half = bool(events) and events[0].on_date == hist[0][0]
    if not is_half and regular > 0 and (1 - current / regular) >= _CURRENT_MIN_OFF:
        is_half = True

    # Current discount depth, populated whenever the item is "on special" at the
    # broader 30% floor — independent of is_half, so scrape() can also emit
    # sub-half (30-47%) rows. The is_half_price flag is decided in scrape() from
    # is_current_half + this pct, preserving the exact half-price set.
    off = (1 - current / regular) if regular > 0 else 0.0
    on_special = off >= _SPECIAL_MIN_OFF
    cur_sale = current if on_special else None
    cur_pct = round(100 * off) if on_special else None

    return ColesProduct(
        coles_id=cid,
        name=name,
        image_url=_image_url(retailer, cid),
        source_product_url=_product_url(retailer, cid, name),
        regular_cents=regular,
        current_sale_cents=cur_sale,
        current_discount_pct=cur_pct,
        is_current_half=is_half,
        category=category_label(raw.get("category"), name),
        events=events,
    )


def parse_products(raw_items: list[dict], *, log: logging.Logger,
                   today: date | None = None, retailer: str = "coles") -> list[ColesProduct]:
    """Parse every product; keep only those with usable price history.

    Marketplace / non-grocery items are dropped here, so nothing downstream
    (specials, catalogue, history backfill, predictor, matcher) ever sees them.
    """
    today = today or datetime.now(timezone.utc).date()
    products: list[ColesProduct] = []
    marketplace = 0
    for raw in raw_items:
        if _is_marketplace(raw, retailer):
            marketplace += 1
            continue
        p = _parse_one(raw, today=today, retailer=retailer)
        if p is not None:
            products.append(p)
    half = sum(1 for p in products if p.is_current_half)
    with_hist = sum(1 for p in products if p.events)
    log.info("hotprices.parsed products=%d currently_half=%d ever_half=%d marketplace_skipped=%d",
             len(products), half, with_hist, marketplace)
    return products


def _most_recent_wednesday(today: date) -> date:
    return today - timedelta(days=(today.weekday() - 2) % 7)


def scrape(log: logging.Logger, *, today: date | None = None,
           retailer: str = "coles") -> ScrapeOutput:
    """This week's authoritative half-price specials for a retailer, from its dump.

    Coles is the primary use (Woolies normally comes from its live API); the
    Woolies path here is the CI fallback when that API is blocked (datacenter IPs).
    """
    run = ScrapeRun(
        source="hotprices",
        started_at=datetime.now(timezone.utc),
        source_url=HOTPRICES_URLS[retailer],
    )
    today = today or datetime.now(timezone.utc).date()
    week_start = _most_recent_wednesday(today)
    week_end = week_start + timedelta(days=6)
    scraped_at = datetime.now(timezone.utc)

    try:
        raw = fetch_dump(retailer, log=log)
        products = parse_products(raw, log=log, today=today, retailer=retailer)
    except Exception as e:  # noqa: BLE001
        log.exception("hotprices.scrape_error retailer=%s", retailer)
        run.finalise(status="failed", items=0, error=str(e))
        return ScrapeOutput(run=run)

    specials: list[WeeklySpecial] = []
    for p in products:
        # Emit anything "on special" (>=30% off). Half-price — flagged
        # current-half AND >=48% off — lands is_half_price=True (byte-identical
        # to the old half-only behaviour). 30-47% lands is_half_price=False for
        # the Specials view and is invisible to the predictor.
        if p.current_sale_cents is None:
            continue
        pct = p.current_discount_pct or 0
        specials.append(WeeklySpecial(
            retailer=retailer,
            product_name=p.name,
            category=p.category,
            regular_price_cents=p.regular_cents,
            sale_price_cents=p.current_sale_cents,
            discount_pct=pct,
            is_half_price=(p.is_current_half and pct >= 48),
            last_halfprice_raw="",
            last_halfprice_weeks_ago=None,
            last_halfprice_retailer=None,
            week_start=week_start,
            week_end=week_end,
            source="hotprices",
            source_url=p.source_product_url or HOTPRICES_URLS[retailer],
            scraped_at=scraped_at,
            image_url=p.image_url,
            retailer_sku=f"{retailer}:{p.coles_id}",
        ))

    half = sum(1 for s in specials if s.is_half_price)
    run.finalise(status="success" if specials else "no_data", items=len(specials))
    run.notes = f"specials={len(specials)} half={half} week_start={week_start}"
    log.info("hotprices.scrape_done specials=%d half=%d week_start=%s",
             len(specials), half, week_start)
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
