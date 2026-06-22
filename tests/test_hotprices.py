"""Half-price derivation — the change-point detection + recency guard +
regular-price estimate that decide what the app calls "half-price". This is the
highest product-accuracy-value logic in the pipeline."""
from datetime import date, timedelta

from src.scrapers import hotprices as hp


class TestImageUrl:
    def test_deterministic_coles_cdn(self):
        assert hp.build_coles_image_url("6238038") == (
            "https://cdn.productimages.coles.com.au/productimages/6/6238038.jpg"
        )


class TestCents:
    def test_dollars_to_cents(self):
        assert hp._cents(3.5) == 350
        assert hp._cents("2.00") == 200

    def test_rejects_zero_and_garbage(self):
        assert hp._cents(0) is None
        assert hp._cents(None) is None
        assert hp._cents("abc") is None


class TestHistoryPoints:
    def test_parses_and_skips_malformed(self):
        raw = {"priceHistory": [
            {"date": "2026-06-10", "price": 5.0},
            {"date": "bad-date", "price": 4.0},   # skipped
            {"date": "2026-05-01"},               # no price -> skipped
            {"date": "2026-04-01", "price": 10.0},
        ]}
        pts = hp._history_points(raw)
        assert pts == [(date(2026, 6, 10), 500), (date(2026, 4, 1), 1000)]

    def test_empty(self):
        assert hp._history_points({}) == []


class TestHalfPriceEvents:
    def test_detects_50pct_drop(self):
        # newest-first: 500 dropped from 1000 = 50% off -> a half-price event.
        hist = [(date(2026, 6, 10), 500), (date(2026, 5, 1), 1000)]
        events = hp._half_price_events(hist)
        assert len(events) == 1
        assert events[0].sale_cents == 500 and events[0].regular_cents == 1000

    def test_ignores_shallow_30pct_drop(self):
        hist = [(date(2026, 6, 10), 700), (date(2026, 5, 1), 1000)]
        assert hp._half_price_events(hist) == []


class TestRegularCents:
    def test_max_within_window(self):
        today = date(2026, 6, 17)
        hist = [(today - timedelta(days=2), 500), (today - timedelta(days=30), 1000)]
        assert hp._regular_cents(hist, today) == 1000

    def test_excludes_points_older_than_window(self):
        today = date(2026, 6, 17)
        # The 2000c point is > 300 days old -> excluded; falls back to recent max.
        hist = [(today - timedelta(days=2), 500),
                (today - timedelta(days=400), 2000)]
        assert hp._regular_cents(hist, today) == 500


class TestParseOneRecencyGuard:
    TODAY = date(2026, 6, 17)

    def _raw(self, drop_days_ago: int):
        return {
            "id": "12345",
            "name": "Leggo's Pasta Sauce 500g",
            "priceHistory": [
                {"date": (self.TODAY - timedelta(days=drop_days_ago)).isoformat(), "price": 2.0},
                {"date": (self.TODAY - timedelta(days=200)).isoformat(), "price": 4.0},
            ],
        }

    def test_recent_half_drop_is_current_half(self):
        p = hp._parse_one(self._raw(3), today=self.TODAY)
        assert p is not None and p.is_current_half is True
        assert p.regular_cents == 400 and p.current_sale_cents == 200

    def test_stale_half_drop_gated_off(self):
        # Same 50%-off price, but the last change was 20 days ago (> 14d guard):
        # the item is sitting at a stable price, not a current special.
        p = hp._parse_one(self._raw(20), today=self.TODAY)
        assert p is not None and p.is_current_half is False

    def test_shallow_special_not_half_but_on_special(self):
        raw = {
            "id": "999", "name": "Tinned Tomatoes",
            "priceHistory": [
                {"date": (self.TODAY - timedelta(days=2)).isoformat(), "price": 7.0},
                {"date": (self.TODAY - timedelta(days=100)).isoformat(), "price": 10.0},
            ],
        }
        p = hp._parse_one(raw, today=self.TODAY)
        assert p.is_current_half is False
        assert p.current_discount_pct == 30  # on special, sub-half

    def test_missing_id_or_name_dropped(self):
        assert hp._parse_one({"name": "x", "priceHistory": []}, today=self.TODAY) is None
        assert hp._parse_one({"id": "1", "priceHistory": []}, today=self.TODAY) is None
