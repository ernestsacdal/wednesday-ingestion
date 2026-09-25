"""enforce_badge_invariant: a not-half row at a half-looking discount would show
a "1/2 price" badge in the app, so the writer never lets one through."""
from datetime import date

from src.db.bulk_writer import enforce_badge_invariant
from src.models import WeeklySpecial

WEEK = date(2026, 9, 23)


def _special(sku, pct, half):
    return WeeklySpecial(
        retailer="coles", product_name=sku, category="Pantry",
        regular_price_cents=1000, sale_price_cents=1000 - pct * 10,
        discount_pct=pct, is_half_price=half, last_halfprice_raw="",
        last_halfprice_weeks_ago=None, last_halfprice_retailer=None,
        week_start=WEEK, week_end=date(2026, 9, 29), source="hotprices",
        source_url="", scraped_at=None, retailer_sku=f"coles:{sku}",
    )


def test_half_rows_always_kept():
    kept, dropped = enforce_badge_invariant([_special("a", 50, True), _special("b", 75, True)])
    assert [s.product_name for s in kept] == ["a", "b"] and dropped == 0


def test_sub_half_specials_kept():
    kept, dropped = enforce_badge_invariant([_special("a", 30, False), _special("b", 47, False)])
    assert len(kept) == 2 and dropped == 0


def test_stale_not_half_at_half_discount_dropped():
    kept, dropped = enforce_badge_invariant(
        [_special("stale", 50, False), _special("edge", 48, False), _special("ok", 50, True)])
    assert [s.product_name for s in kept] == ["ok"] and dropped == 2
