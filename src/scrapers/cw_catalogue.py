"""Chemist Warehouse catalogue through its publisher's public feed (ADR-0004).

Chemist Warehouse (CW) embeds its fortnightly catalogue, Thursday to the
Wednesday a fortnight later, from Nexus Publications on
chemistwarehouse.com.au/catalogue. The publisher's feed lists every product
region on every page: name, brand, price, CW's own claim ("$39.00 OFF RRP
1/2 PRICE OFF RRP"), a category id in the Lasoo taxonomy, and a link to the
exact product (/buy/<id>/<slug>).

    client/<CLIENT>/catalogues/json           catalogues currently listed
    catalogue/<nid>/page-images/json          one entry per page
    rpms_catalogue/<nid>/page/<n>/10/regions  regions of pages n..n+10

We keep exact products only (a /buy/<id> link); range lines ("Sukin Range, up
to 1/2 price") have no single product. CW prices its deals off RRP, not off
what it normally charges, so the RRP and claim are kept as CW states them and
the real saving is measured elsewhere against CW's usual price. Product data
lands a few days after a catalogue starts: a catalogue with pages but no
regions yet is "pending", not an error.

Public surface:
    list_catalogues(session, log) -> list[Catalogue]
    fetch_catalogue(catalogue, session, log) -> CatalogueFetch
    parse_region(region, *, page) -> CwDeal | None
    parse_claim(text, price_cents) -> Claim
    image_url(cw_id) / product_url(cw_id, slug) / build_session()
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

from src.scrapers.hotprices import _keyword_category

CLIENT_ID = "3476"
_FEED = "https://node.nexuspublications.com.au/"
_HEADERS = {
    # Honest identification; the feed and CW's /buy/ pages both accept it.
    "User-Agent": (
        "WednesdayApp/1.4 (+https://wednesday.ernestmikhail.com; "
        "weekly price checks for a free price-tracking app)"
    ),
    "Referer": "https://app.nexuspublications.com.au/",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}
_PAGES_PER_CALL = 10
_DELAY_S = 1.0

_BUY_RE = re.compile(r"chemistwarehouse\.com\.au/buy/(\d{1,9})(?:/([a-z0-9-]+))?", re.I)
_OFF_RRP_AMOUNT = re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*off\s*rrp", re.I)
_OFF_EDLP_AMOUNT = re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*off\s*edlp", re.I)
_PCT_OFF_RRP = re.compile(r"\b([1-9][0-9]?)\s*%\s*off\s*rrp", re.I)
_HALF_RRP = re.compile(r"1/2\s*price\s*off\s*rrp", re.I)
_UP_TO = re.compile(r"up\s*to\s*1/2", re.I)
_BETTER_THAN = re.compile(r"better\s*than\s*1/2", re.I)

_CATEGORIES: dict[str, str] = {
    k: v for k, v in json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "cw_categories.json").read_text()
    ).items() if not k.startswith("_")
}


@dataclass(frozen=True)
class Catalogue:
    nid: str
    title: str
    starts_on: date
    ends_on: date


@dataclass(frozen=True)
class Claim:
    rrp_cents: int | None
    edlp_cents: int | None   # CW's own everyday price, from "$X OFF EDLP"
    half_rrp: bool           # CW says "1/2 price off RRP" (not "up to")
    off_edlp: bool


@dataclass(frozen=True)
class CwDeal:
    cw_id: str
    slug: str | None
    name: str
    brand: str | None
    price_cents: int
    rrp_cents: int | None
    edlp_cents: int | None
    claim: str
    half_rrp: bool
    off_edlp: bool
    category: str
    page: int | None


@dataclass
class CatalogueFetch:
    catalogue: Catalogue
    pages: int = 0
    regions: int = 0
    products: int = 0
    deals: list[CwDeal] = field(default_factory=list)
    complete: bool = False
    error: str | None = None

    @property
    def half_rrp(self) -> int:
        return sum(1 for d in self.deals if d.half_rrp)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def image_url(cw_id: str) -> str:
    return f"https://static.chemistwarehouse.com.au/ams/media/pi/{cw_id}/hero_800.jpg"


def product_url(cw_id: str, slug: str | None) -> str:
    base = f"https://www.chemistwarehouse.com.au/buy/{cw_id}"
    return f"{base}/{slug}" if slug else base


def _cents(text) -> int | None:
    try:
        c = round(float(str(text).replace(",", "").strip()) * 100)
    except (TypeError, ValueError):
        return None
    return c if c > 0 else None


def parse_claim(text: str, price_cents: int) -> Claim:
    """CW's wording -> RRP, its own everyday price, and the claim flags.

    "$39.00 OFF RRP" gives RRP = price + $39; "1/2 PRICE OFF RRP" alone gives
    RRP = 2 x price; "40% OFF RRP" gives price / 0.6; "$X OFF EDLP" is off CW's
    own everyday low price, which is then price + X.
    """
    t = " ".join((text or "").split())
    rrp: int | None = None
    if m := _OFF_RRP_AMOUNT.search(t):
        rrp = price_cents + round(float(m.group(1)) * 100)
    elif _HALF_RRP.search(t) and not _UP_TO.search(t) and not _BETTER_THAN.search(t):
        rrp = price_cents * 2
    elif m := _PCT_OFF_RRP.search(t):
        rrp = round(price_cents / (1 - int(m.group(1)) / 100))
    edlp = None
    if m := _OFF_EDLP_AMOUNT.search(t):
        edlp = price_cents + round(float(m.group(1)) * 100)
    half = bool(_HALF_RRP.search(t)) and not _UP_TO.search(t)
    return Claim(rrp_cents=rrp, edlp_cents=edlp, half_rrp=half, off_edlp=edlp is not None)


def category_for(category_id, name: str) -> str:
    label = _CATEGORIES.get(str(category_id or "").strip())
    return label or _keyword_category(name)


def parse_region(region: dict, *, page: int | None = None) -> CwDeal | None:
    """One feed region -> an exact-product deal, or None (not a product, a
    range, or no usable price)."""
    if (region.get("field_region_type") or "") != "product":
        return None
    m = _BUY_RE.search(region.get("field_product_buynow") or "")
    if not m:
        return None  # a range or brand page, not one product
    if (region.get("field_currency_symbol") or "").strip().lower().startswith("from"):
        return None  # "From $9.47": a range price
    price = _cents(region.get("field_product_price"))
    if price is None:
        return None
    name = " ".join((region.get("field_product_title") or region.get("field_region_hover_text") or "").split())
    if not name:
        return None
    claim_text = " ".join((region.get("field_product_description") or "").split())
    claim = parse_claim(claim_text, price)
    brand = (region.get("field_product_brand") or "").strip() or None
    return CwDeal(
        cw_id=m.group(1),
        slug=(m.group(2) or None),
        name=name,
        brand=brand,
        price_cents=price,
        rrp_cents=claim.rrp_cents,
        edlp_cents=claim.edlp_cents,
        claim=claim_text,
        half_rrp=claim.half_rrp,
        off_edlp=claim.off_edlp,
        category=category_for(region.get("field_category_id"), name),
        page=page,
    )


def _get(session: requests.Session, path: str, log: logging.Logger):
    url = _FEED + path
    try:
        r = session.get(url, timeout=30)
    except Exception as e:  # noqa: BLE001 — callers treat None as a soft failure
        log.warning("cw.request_error path=%s err=%s", path.split("?")[0], e)
        return None
    if r.status_code != 200:
        log.warning("cw.http_status path=%s status=%s", path.split("?")[0], r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        log.warning("cw.not_json path=%s", path.split("?")[0])
        return None


def _date(value) -> date | None:
    try:
        return datetime.fromisoformat(str(value)[:16]).date()
    except (TypeError, ValueError):
        return None


def list_catalogues(session: requests.Session, log: logging.Logger) -> list[Catalogue]:
    """Catalogues the publisher lists for CW right now (usually the running one)."""
    data = _get(session, f"client/{CLIENT_ID}/catalogues/json?_format=json", log)
    out: list[Catalogue] = []
    for row in data or []:
        nid, start, end = row.get("nid_1"), _date(row.get("start")), _date(row.get("finish"))
        if nid and start and end:
            out.append(Catalogue(str(nid), (row.get("title") or "").strip(), start, end))
    return out


def fetch_catalogue(catalogue: Catalogue, session: requests.Session,
                    log: logging.Logger) -> CatalogueFetch:
    """Every exact-product deal in one catalogue; complete=True only when every
    page block was fetched and at least one region was published."""
    out = CatalogueFetch(catalogue=catalogue)
    pages = _get(session, f"catalogue/{catalogue.nid}/page-images/json?_format=json", log)
    if not isinstance(pages, list) or not pages:
        out.error = "no_pages"
        return out
    out.pages = len(pages)
    seen: dict[str, CwDeal] = {}
    region_ids: set[str] = set()
    ok = True
    for start in range(0, out.pages, _PAGES_PER_CALL):
        time.sleep(_DELAY_S)
        block = _get(session, f"rpms_catalogue/{catalogue.nid}/page/{start}/{_PAGES_PER_CALL}/regions", log)
        if not isinstance(block, dict):
            ok = False
            continue
        for key, regions in block.items():
            for region in regions or []:
                # Blocks overlap by a page and the feed repeats regions (each
                # comes back about twice): count each region id once.
                rid = str(region.get("id") or "")
                if rid and rid in region_ids:
                    continue
                region_ids.add(rid)
                raw_page = str(region.get("page") or key)
                page = int(raw_page) if raw_page.isdigit() else None
                out.regions += 1
                if (region.get("field_region_type") or "") == "product":
                    out.products += 1
                deal = parse_region(region, page=page)
                # A product shown twice keeps its lower price.
                if deal and (deal.cw_id not in seen or deal.price_cents < seen[deal.cw_id].price_cents):
                    seen[deal.cw_id] = deal
    out.deals = list(seen.values())
    if out.regions == 0:
        out.error = "regions_pending"
    out.complete = ok and out.regions > 0
    log.info("cw.fetched catalogue=%s title=%r pages=%d regions=%d products=%d exact=%d half_rrp=%d complete=%s",
             catalogue.nid, catalogue.title, out.pages, out.regions, out.products,
             len(out.deals), out.half_rrp, out.complete)
    return out
