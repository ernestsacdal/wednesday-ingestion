"""Woolworths browse-API row mapping and crawl completeness (pure — no network)."""
from datetime import date, datetime, timezone

from src.scrapers import woolies_specials as w

WEEK = date(2026, 9, 23)
NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _tile(**over):
    tile = {
        "Name": "Fairy Platinum Dishwashing Tablets", "Stockcode": 154829,
        "Price": 23.0, "WasPrice": 46.0, "IsHalfPrice": True, "IsAvailable": True,
        "IsMarketProduct": False, "Brand": "Fairy", "Barcode": "8001090000000",
        "PackageSize": "84 pack", "Department": "Cleaning",
    }
    tile.update(over)
    return tile


def _special(**over):
    return w._to_special(_tile(**over), week_start=WEEK, week_end=date(2026, 9, 29), scraped_at=NOW)


def test_maps_prices_key_and_attributes():
    sp = _special()
    assert (sp.sale_price_cents, sp.regular_price_cents, sp.discount_pct) == (2300, 4600, 50)
    assert sp.retailer_sku == "woolworths:154829"
    assert (sp.brand, sp.barcode, sp.size) == ("Fairy", "8001090000000", "84 pack")
    assert sp.is_half_price


def test_half_flag_comes_from_woolworths_not_rounding():
    # 49.6% off, in the Half Price node: the old round(pct) >= 50 said "not half".
    assert _special(Price=2.52, WasPrice=5.0, IsHalfPrice=True).is_half_price
    assert not _special(Price=2.5, WasPrice=5.0, IsHalfPrice=False).is_half_price


def test_percentage_fallback_when_flag_missing():
    tile = _tile(Price=2.6, WasPrice=5.0)
    del tile["IsHalfPrice"]
    sp = w._to_special(tile, week_start=WEEK, week_end=date(2026, 9, 29), scraped_at=NOW)
    assert sp.is_half_price  # 48% off


def test_unavailable_and_marketplace_rows_skipped():
    assert _special(Price=None, IsAvailable=False) is None
    assert _special(IsMarketProduct=True) is None


def test_dedup_is_by_stockcode_so_same_name_products_survive():
    a, b = _tile(Stockcode=1), _tile(Stockcode=2)
    ka = w._dedup_key(a, _special(Stockcode=1))
    kb = w._dedup_key(b, _special(Stockcode=2))
    assert ka != kb


def test_completeness_and_page_bound():
    assert w.is_complete(1729, 1729)
    assert w.is_complete(1700, 1729)       # >= 98%
    assert not w.is_complete(1640, 1839)   # the old name-dedup shortfall
    assert not w.is_complete(10, None)
    assert w._max_pages(1729) == 49 + w._PAGE_SLACK
    assert w._max_pages(None) == w._MAX_PAGES_CAP
    assert w._max_pages(10_000) == w._MAX_PAGES_CAP
