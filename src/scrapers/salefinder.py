"""Coles weekly catalogue via SaleFinder — a cloud-reachable Coles ground truth.

Coles has no usable live price API (the BFF returns 403; the site is behind
Imperva). But Coles' printed weekly catalogue is digitised by SaleFinder, whose
public webservice exposes it as structured JSON with an EXPLICIT half-price flag
("1/2 PRICE" in each item's description) plus Was/Now prices — and, crucially,
each item's SKU (minus a trailing "P") IS the real Coles product id, so it joins
cleanly to our `coles:<id>` keys.

Reachable from any IP (it's a CDN-backed API, not WAF-gated like coles.com.au),
so this runs in the cloud cron. It is the catalogue SUBSET (~100-300 featured
items/week), not the full half-price set — so it's a ground-truth SAMPLE for
measuring Coles recall + a precision sample, and for confirming half-price flags,
not a complete source.

Discovered API (Coles retailerId=148, public embed apiKey):
    sales/retailer/?id=148&storeId=<store>   -> current catalogues (saleId, dates)
    products/sale/?id=<saleId>&storeId=<store> -> items (itemName, description,
                                                  SKU, prices[])
Public surface:
    fetch_coles_catalogue(log) -> CatalogueResult
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import requests

_BASE = "https://webservice.salefinder.com.au/index.php/api/"
_APIKEY = "c0l8sDE5683419EEF6"   # public key embedded in SaleFinder's Coles widget
_RETAILER_ID = 148               # Coles
_STORE_ID = "8442"               # Coles World Square (NSW Metro) — half-price is
                                 # national, so one region's catalogue is representative
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://embed.salefinder.com.au/coles/",
}
_HALF_RE = re.compile(r"1/2\s*price|half\s*price", re.I)
_WAS_RE = re.compile(r"was\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


@dataclass
class CatalogueItem:
    coles_id: str        # real Coles product id (SaleFinder SKU minus trailing letters)
    name: str
    is_half: bool
    sale_cents: int | None
    was_cents: int | None
    discount_desc: str   # raw description, e.g. "1/2 PRICE" or "40% OFF"


@dataclass
class CatalogueResult:
    items: list[CatalogueItem] = field(default_factory=list)
    sale_ids: list[str] = field(default_factory=list)
    week_start: date | None = None
    error: str | None = None

    @property
    def half(self) -> list[CatalogueItem]:
        return [i for i in self.items if i.is_half]


def _get_jsonp(path: str, log: logging.Logger) -> dict | None:
    url = f"{_BASE}{path}&apikey={_APIKEY}&format=jsonp"
    try:
        text = requests.get(url, headers=_HEADERS, timeout=25).text
    except Exception as e:  # noqa: BLE001 — caller treats None as a soft failure
        log.warning("salefinder.request_error path=%s err=%s", path.split("?")[0], e)
        return None
    m = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
    payload = m.group(1) if m else text
    try:
        import json
        return json.loads(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("salefinder.parse_error path=%s err=%s", path.split("?")[0], e)
        return None


def _cents(v) -> int | None:
    try:
        c = round(float(v) * 100)
        return c if c > 0 else None
    except (TypeError, ValueError):
        return None


def _coles_id(sku: str | None) -> str | None:
    """SaleFinder SKU '329607P' -> Coles product id '329607'."""
    if not sku:
        return None
    digits = re.sub(r"[^0-9]", "", sku)
    return digits or None


def _current_sale_ids(log: logging.Logger, today: date) -> list[str]:
    data = _get_jsonp(f"sales/retailer/?id={_RETAILER_ID}&storeId={_STORE_ID}", log)
    if not data:
        return []
    out: list[str] = []
    for wrap in data.get("items") or []:
        s = wrap.get("items", wrap)
        sid = s.get("saleId")
        start, end = s.get("startDate"), s.get("endDate")
        if not sid or not start or not end:
            continue
        try:
            if date.fromisoformat(start) <= today <= date.fromisoformat(end):
                out.append(str(sid))
        except (TypeError, ValueError):
            continue
    return out


def fetch_coles_catalogue(log: logging.Logger, *, today: date | None = None) -> CatalogueResult:
    """Current-week Coles catalogue items (half-price flagged), joined-by-id-ready."""
    today = today or datetime.now(timezone.utc).date()
    sale_ids = _current_sale_ids(log, today)
    if not sale_ids:
        return CatalogueResult(error="no_current_catalogue")

    by_id: dict[str, CatalogueItem] = {}
    for sid in sale_ids:
        data = _get_jsonp(f"products/sale/?id={sid}&storeId={_STORE_ID}", log)
        if not data:
            continue
        for wrap in data.get("items") or []:
            x = wrap.get("items", wrap)
            cid = _coles_id(x.get("SKU"))
            name = html.unescape((x.get("itemName") or "").strip())
            if not cid or not name:
                continue
            desc = html.unescape((x.get("description") or "")).strip()
            prices = x.get("prices") or []
            sale_cents = was_cents = None
            if prices:
                p0 = prices[0]
                sale_cents = _cents(p0.get("priceSale") or p0.get("priceReg"))
                m = _WAS_RE.search(p0.get("priceOptionDesc") or "")
                if m:
                    was_cents = _cents(m.group(1))
            # de-dupe: keep the half-price reading if any catalogue marks it half
            item = CatalogueItem(
                coles_id=cid, name=name, is_half=bool(_HALF_RE.search(desc)),
                sale_cents=sale_cents, was_cents=was_cents, discount_desc=desc[:40],
            )
            existing = by_id.get(cid)
            if existing is None or (item.is_half and not existing.is_half):
                by_id[cid] = item

    # Coles promo week starts Wednesday; align to the catalogue's start.
    week_start = today - __import__("datetime").timedelta(days=(today.weekday() - 2) % 7)
    res = CatalogueResult(items=list(by_id.values()), sale_ids=sale_ids, week_start=week_start)
    log.info("salefinder.coles sale_ids=%s items=%d half=%d",
             ",".join(sale_ids), len(res.items), len(res.half))
    return res


if __name__ == "__main__":
    import sys
    from src.scrapers.base import configure_logging
    _log = configure_logging(verbose=True)
    r = fetch_coles_catalogue(_log)
    if r.error:
        print("error:", r.error); sys.exit(1)
    print(f"\nsale_ids={r.sale_ids} items={len(r.items)} half={len(r.half)}")
    for i in r.half[:8]:
        was = f"${i.was_cents/100:.2f}" if i.was_cents else "?"
        sale = f"${i.sale_cents/100:.2f}" if i.sale_cents else "?"
        print(f"  {i.coles_id:<9} {i.name[:44]:<44} {i.discount_desc:<10} was {was} now {sale}")
