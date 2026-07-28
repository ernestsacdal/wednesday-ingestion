"""The catalogue upsert must refresh price + name on existing rows.

Regression for the Sunsilk stale-price incident (2026-07-28): supplier price
rises never reached products that were not currently on special, because the
shared product upsert's ON CONFLICT clause updated category/image/last_seen
but never regular_price_cents or name — freezing both at first insert.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from src.backfill_history import _upsert_products

log = logging.getLogger("test")


class FakeCursor:
    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, list(params or [])))


def _product(cid="463191", name="Sunsilk Conditioner", regular=1500):
    return SimpleNamespace(
        coles_id=cid,
        name=name,
        category="Health & Beauty",
        regular_cents=regular,
        image_url=f"https://img/{cid}.jpg",
        source_product_url=f"https://woolworths.com.au/p/{cid}",
    )


def _conflict_clause(sql: str) -> str:
    return sql.split("on conflict", 1)[1]


class TestUpsertRefreshesExistingRows:
    def test_price_updates_on_conflict(self):
        cur = FakeCursor()
        _upsert_products(cur, [_product()], "woolworths", log)
        clause = _conflict_clause(cur.calls[0][0])
        assert "regular_price_cents = case" in clause
        assert "excluded.regular_price_cents" in clause

    def test_name_updates_on_conflict(self):
        cur = FakeCursor()
        _upsert_products(cur, [_product()], "woolworths", log)
        clause = _conflict_clause(cur.calls[0][0])
        assert "name = case" in clause
        assert "excluded.name" in clause

    def test_zero_price_guarded(self):
        # excluded.regular_price_cents must only win when positive.
        cur = FakeCursor()
        _upsert_products(cur, [_product()], "woolworths", log)
        clause = _conflict_clause(cur.calls[0][0])
        assert "excluded.regular_price_cents > 0" in clause

    def test_params_match_values_row_shape(self):
        cur = FakeCursor()
        _upsert_products(cur, [_product(regular=1500)], "woolworths", log)
        _, params = cur.calls[0]
        assert params == [
            "woolworths",
            "woolworths:463191",
            "Sunsilk Conditioner",
            "Health & Beauty",
            1500,
            "https://img/463191.jpg",
            "https://woolworths.com.au/p/463191",
        ]

    def test_dedup_by_sku_keeps_last(self):
        cur = FakeCursor()
        _upsert_products(
            cur,
            [_product(regular=1350), _product(regular=1500)],
            "woolworths",
            log,
        )
        _, params = cur.calls[0]
        # One row for the duplicated sku, carrying the last-seen value.
        assert params.count("woolworths:463191") == 1
        assert 1500 in params and 1350 not in params
