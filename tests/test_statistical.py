"""Predictor math — the cycle interval/confidence/window logic that backs the
in-app "Going half-price" verdict and the published accuracy numbers."""
from datetime import date, timedelta

from src.prediction.statistical import (
    MAX_WINDOW_HALFWIDTH_WEEKS,
    MIN_CYCLE_INTERVAL_WEEKS,
    STDDEV_FLOOR_WEEKS_LOW_N,
    STDDEV_FLOOR_WEEKS_N1,
    _confidence_tier,
    _derive_intervals,
    _predict_for_product,
)


class TestConfidenceTier:
    def test_high_at_and_above_075(self):
        assert _confidence_tier(0.75) == "high"
        assert _confidence_tier(0.95) == "high"

    def test_medium_band(self):
        assert _confidence_tier(0.74) == "medium"
        assert _confidence_tier(0.45) == "medium"

    def test_low_below_045(self):
        assert _confidence_tier(0.44) == "low"
        assert _confidence_tier(0.0) == "low"


class TestDeriveIntervals:
    def test_regular_three_weekly_cycle(self):
        entries = [
            (date(2026, 1, 7), None),
            (date(2026, 1, 28), None),
            (date(2026, 2, 18), None),
        ]
        assert _derive_intervals(entries) == [3, 3]

    def test_hint_expands_to_prior_sale(self):
        # A single sale with a "3 weeks ago" hint implies one prior sale.
        assert _derive_intervals([(date(2026, 2, 18), 3)]) == [3]

    def test_near_duplicate_dates_deduped(self):
        # Two sale dates within 7 days are the same event, not a 0-week cycle.
        entries = [(date(2026, 1, 7), None), (date(2026, 1, 10), None),
                   (date(2026, 1, 28), None)]
        assert _derive_intervals(entries) == [3]

    def test_sub_minimum_intervals_filtered(self):
        # A 1-week gap is a multi-week sale, not a cycle — dropped.
        entries = [(date(2026, 1, 7), None), (date(2026, 1, 14), None)]
        assert all(w >= MIN_CYCLE_INTERVAL_WEEKS for w in _derive_intervals(entries))
        assert _derive_intervals(entries) == []

    def test_empty_and_single(self):
        assert _derive_intervals([]) == []
        assert _derive_intervals([(date(2026, 1, 7), None)]) == []


class TestPredictForProduct:
    TODAY = date(2026, 6, 17)

    def test_gated_when_below_min_cycles(self):
        assert _predict_for_product([3], date(2026, 6, 1), today=self.TODAY, min_cycles=2) is None

    def test_single_interval_uses_n1_stddev_floor(self):
        res = _predict_for_product([3], date(2026, 6, 10), today=self.TODAY, min_cycles=1)
        assert res is not None
        mean, stddev, win_start, win_end = res
        assert mean == 3
        assert stddev == STDDEV_FLOOR_WEEKS_N1

    def test_low_n_zero_variance_gets_floor(self):
        # 2-3 perfectly-regular intervals must not yield a zero-width window.
        res = _predict_for_product([3, 3], date(2026, 6, 10), today=self.TODAY, min_cycles=1)
        _, stddev, _, _ = res
        assert stddev == STDDEV_FLOOR_WEEKS_LOW_N

    def test_halfwidth_capped(self):
        # A wildly dispersed history can't produce a > MAX_HALFWIDTH window.
        res = _predict_for_product([2, 30, 2, 30], date(2026, 6, 10), today=self.TODAY, min_cycles=1)
        _, _, win_start, win_end = res
        half_days = (win_end - win_start).days / 2
        assert half_days <= MAX_WINDOW_HALFWIDTH_WEEKS * 7 + 1

    def test_slipped_beat_rolls_forward_one_interval(self):
        # "Slipped a beat": last_sale ~6wk ago, mean 3wk -> the naive next-sale
        # window already ended before today, so it rolls forward exactly one
        # mean interval into the future. (A far-stale product is deliberately
        # NOT looped forward — that would be false precision; the app's
        # isExpiredPrediction guard surfaces those as "Window passed".)
        last_sale = self.TODAY - timedelta(weeks=6)
        res = _predict_for_product([3, 3], last_sale, today=self.TODAY, min_cycles=1)
        _, _, _, win_end = res
        assert win_end >= self.TODAY
