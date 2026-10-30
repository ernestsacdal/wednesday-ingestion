"""Chemist Warehouse catalogue parsing (src/scrapers/cw_catalogue.py) and the
usual-price page parser (src/cw_usual_prices.py). Pure logic, no network."""
import logging
from datetime import date

from src.cw_usual_prices import parse_page
from src.scrapers import cw_catalogue
from src.scrapers.cw_catalogue import (
    Catalogue,
    category_for,
    fetch_catalogue,
    image_url,
    parse_claim,
    parse_region,
    product_url,
)


def region(**over):
    base = {
        "id": "39400172",
        "page": "7",
        "field_region_type": "product",
        "field_product_buynow": "https://www.chemistwarehouse.com.au/buy/168017/dr-lewinns-plump-lift-firming-neck-cream-50ml",
        "field_product_title": "Dr. LeWinn's Plump + Lift Firming Neck Cream 50ml",
        "field_product_brand": "Dr. LeWinn's",
        "field_product_price": "38.99",
        "field_currency_symbol": "$",
        "field_product_description": "$39.00 OFF RRP\n\n1/2 PRICE OFF RRP",
        "field_category_id": "567",
    }
    base.update(over)
    return base


def test_exact_product_deal():
    d = parse_region(region(), page=7)
    assert d.cw_id == "168017"
    assert d.slug == "dr-lewinns-plump-lift-firming-neck-cream-50ml"
    assert d.price_cents == 3899
    assert d.rrp_cents == 7799          # price + "$39.00 OFF RRP"
    assert d.half_rrp is True
    assert d.off_edlp is False
    assert d.category == "Skin Care"
    assert d.claim == "$39.00 OFF RRP 1/2 PRICE OFF RRP"


def test_ranges_and_non_products_are_skipped():
    # A brand range page, not one product
    assert parse_region(region(field_product_buynow="https://www.chemistwarehouse.com.au/shop-online/902/sukin")) is None
    # "From $9.47": a range price
    assert parse_region(region(field_currency_symbol="From $")) is None
    # Not a product region, or no price
    assert parse_region(region(field_region_type="link")) is None
    assert parse_region(region(field_product_price="")) is None


def test_claims():
    # 1/2 off RRP with no amount: RRP is twice the price
    c = parse_claim("1/2 PRICE OFF RRP", 1149)
    assert (c.rrp_cents, c.half_rrp) == (2298, True)
    # A percentage off RRP
    assert parse_claim("$24.00 OFF RRP 40% Off RRP", 3599).rrp_cents == 5999
    assert parse_claim("40% Off RRP", 3600).rrp_cents == 6000
    # Off CW's own everyday price: that price is known, and it's no RRP claim
    e = parse_claim("$6.60 OFF EDLP", 1439)
    assert (e.edlp_cents, e.off_edlp, e.rrp_cents, e.half_rrp) == (2099, True, None, False)
    # "Up to" is not a claim for this product; "better than" is still half off RRP
    assert parse_claim("UP TO 1/2 PRICE OFF RRP", 947).half_rrp is False
    assert parse_claim("BETTER THAN 1/2 PRICE OFF RRP", 1000).half_rrp is True
    assert parse_claim("BETTER THAN 1/2 PRICE OFF RRP", 1000).rrp_cents is None
    # No claim at all
    assert parse_claim("", 499) == parse_claim("   ", 499)
    assert parse_claim("", 499).rrp_cents is None


def test_categories_map_then_fall_back_to_keywords():
    assert category_for("525", "Swisse Ultiboost Sleep") == "Vitamins & Supplements"
    assert category_for("479", "Calvin Klein Eternity EDP 100ml") == "Fragrance"
    assert category_for(None, "Totally unknown widget") == "Uncategorised"


def test_urls():
    assert image_url("168017") == "https://static.chemistwarehouse.com.au/ams/media/pi/168017/hero_800.jpg"
    assert product_url("168017", None) == "https://www.chemistwarehouse.com.au/buy/168017"


def test_usual_price_page():
    html = ('..."prices":[{"sku":"2743895","price":{"value":{"amount":65.99,"currencyCode":"AUD"},'
            '"type":"cwr-au-price","rrp":{"amount":77.99,"currencyCode":"AUD"}}}],"availability":...')
    assert parse_page(html) == (6599, 7799)
    assert parse_page("<html>no data</html>") == (None, None)


class _Feed:
    """Stands in for the publisher's feed: 3 pages, one block of regions."""

    def __init__(self, regions):
        self.regions = regions

    def get(self, url, timeout=None):
        data = [{"page": n} for n in range(3)] if "page-images" in url else {"0": self.regions}
        return type("Resp", (), {"status_code": 200, "json": lambda _self: data})()


def test_catalogue_without_product_regions_is_pending(monkeypatch):
    monkeypatch.setattr(cw_catalogue, "_DELAY_S", 0)
    cat = Catalogue("38855192", "Test", date(2026, 10, 29), date(2026, 11, 11))
    log = logging.getLogger("test")
    # A new catalogue's cover links arrive before its products
    links = [{"id": "1", "field_region_type": "link"}, {"id": "2", "field_region_type": "link"}]
    f = fetch_catalogue(cat, _Feed(links), log)
    assert (f.regions, f.products, f.complete, f.error) == (2, 0, False, "regions_pending")
    # Repeated regions count once
    f = fetch_catalogue(cat, _Feed([region(), region()]), log)
    assert (f.regions, f.products, len(f.deals), f.complete, f.error) == (1, 1, 1, True, None)
