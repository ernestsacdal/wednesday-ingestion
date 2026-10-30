"""Weekly digest copy (src/send_alerts.py build_message), including the
Chemist Warehouse sentence (ADR-0004: never called half price)."""
from datetime import date

from src.send_alerts import build_message, cw_in_digest


def test_half_price_only_is_unchanged():
    msg = build_message("tok", ["Tim Tams", "Weet-Bix"])
    assert msg["title"] == "Half-price this week"
    assert msg["body"] == "2 of your items are half-price this week: Tim Tams, Weet-Bix."
    assert build_message("tok", ["Tim Tams"])["body"] == "1 of your items is half-price this week: Tim Tams."


def test_chemist_warehouse_gets_its_own_sentence():
    msg = build_message("tok", ["Tim Tams"], ["Swisse Ultiboost Sleep"], date(2026, 11, 11))
    assert msg["title"] == "Half-price this week"
    assert msg["body"] == (
        "1 of your items is half-price this week: Tim Tams. "
        "Also on special at Chemist Warehouse: Swisse Ultiboost Sleep."
    )


def test_chemist_warehouse_only():
    one = build_message("tok", [], ["Swisse Ultiboost Sleep"], date(2026, 11, 11))
    assert one["title"] == "On special this week"
    assert one["body"] == "Swisse Ultiboost Sleep is on special at Chemist Warehouse until Wed 11 Nov."
    two = build_message("tok", [], ["Swisse Sleep", "Aveeno Lotion"], date(2026, 11, 11))
    assert two["body"] == (
        "2 of your items are on special at Chemist Warehouse until Wed 11 Nov: Swisse Sleep, Aveeno Lotion."
    )
    assert "half" not in two["body"].lower()


def test_chemist_warehouse_rides_the_wednesday_alert_only():
    assert cw_in_digest(date(2026, 11, 4))                # Wednesday
    assert not cw_in_digest(date(2026, 10, 31))           # Saturday: the catalogue's data just landed
    assert cw_in_digest(date(2026, 10, 31), force=True)   # --force, for testing
