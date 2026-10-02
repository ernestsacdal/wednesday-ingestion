"""Woolworths hierarchy -> app category, and which source may set a category."""
import json
import logging
from types import SimpleNamespace

from src.backfill_history import _upsert_products as upsert_catalogue
from src.db.bulk_writer import category_is_authoritative
from src.scrapers.woolies_taxonomy import category_from_attributes as cat


def _attrs(sap_cat=None, sap_sub=None, aisles=None):
    a = {"sapcategoryname": sap_cat, "sapsubcategoryname": sap_sub}
    if aisles is not None:
        a["piescategorynamesjson"] = json.dumps(aisles)
    return a


def test_subcategory_beats_the_broad_category():
    # TOILETRIES spans cosmetics, hair and period care.
    assert cat(_attrs("TOILETRIES", "COSMETICS")) == "Cosmetics"
    assert cat(_attrs("TOILETRIES", "SUN CARE")) == "Skin Care"
    assert cat(_attrs("TOILETRIES", "SOMETHING NEW")) == "Beauty & Personal Care"


def test_fixes_the_dump_labels_that_misfiled_woolworths_lines():
    assert cat(_attrs("HEALTH CARE", "VITAMINS")) == "Vitamins & Supplements"  # was Confectionery
    assert cat(_attrs("SNACKS", "CHIPS - SHARING")) == "Savoury Snacks"         # was Frozen Chips
    assert cat(_attrs("DOMESTICWARE", "STORAGE")) == "Kitchenware & Storage"    # was Baby Accessories


def test_case_and_whitespace_insensitive():
    assert cat(_attrs(" confectionery ", "confec sharing - sugar")) == "Confectionery"


def test_aisle_fallback_only_on_an_exact_known_label():
    # Unmapped hierarchy: a promotional aisle is skipped, a known label is used.
    assert cat(_attrs("NEW DEPT", None, ["Halloween", "Cosmetics"])) == "Cosmetics"
    assert cat(_attrs(None, None, ["Lunch Box", "Snacks"])) == "Uncategorised"


def test_unknown_or_missing_is_honestly_uncategorised():
    assert cat(None) == "Uncategorised"
    assert cat({}) == "Uncategorised"
    assert cat(_attrs("NEW DEPT", "NEW SUB")) == "Uncategorised"
    assert cat({"piescategorynamesjson": "not json"}) == "Uncategorised"


def test_only_woolworths_own_feed_overrides_a_woolworths_category():
    assert category_is_authoritative("woolworths", "woolies_catalogue")
    assert not category_is_authoritative("woolworths", "hotprices")
    assert category_is_authoritative("coles", "hotprices")


class _Cursor:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)


def _product():
    return SimpleNamespace(coles_id="1", name="X", category="Confectionery", regular_cents=100,
                           image_url=None, source_product_url=None)


def test_catalogue_load_only_fills_a_missing_woolworths_category():
    log = logging.getLogger("test")
    woolies, coles = _Cursor(), _Cursor()
    upsert_catalogue(woolies, [_product()], "woolworths", log)
    upsert_catalogue(coles, [_product()], "coles", log)
    assert "products.category is null or products.category = 'Uncategorised'" in woolies.sql[0]
    assert "products.category is null" not in coles.sql[0]
