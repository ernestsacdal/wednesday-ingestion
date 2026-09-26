"""Prediction v2 (src/prediction/hazard.py, series.py) — pure, hand-checkable cases."""
from datetime import date

import pytest

from src.prediction import hazard as hz
from src.prediction import series as ser


class TestSeries:
    def test_half_run_lasts_until_the_next_change_capped(self):
        wed = date(2026, 7, 1)
        hist = [(date(2026, 6, 3), 400), (wed, 200), (date(2026, 7, 15), 400)]
        w = ser.week_idx(wed)
        assert ser.half_weeks(hist, today_week=w + 10) == {w, w + 1}      # back up 15 Jul
        stale = [(date(2026, 6, 3), 400), (wed, 200)]                     # never changed again
        assert ser.half_weeks(stale, today_week=w + 10) == {w, w + 1, w + 2}
        assert ser.half_weeks(stale, today_week=w) == {w}                 # not past today

    def test_small_drops_are_not_half_price(self):
        hist = [(date(2026, 6, 3), 400), (date(2026, 7, 1), 250)]
        assert ser.half_weeks(hist, today_week=ser.week_idx(date(2026, 8, 1))) == set()

    def test_run_starts_and_truth_overlay(self):
        assert ser.run_starts({3, 4, 9, 12, 13}) == [3, 9, 12]
        series = {"a": {5}, "b": set()}
        ser.overlay_truth(series, 5, {"b"}, {"a", "b"})
        assert series == {"a": set(), "b": {5}}


class TestGapModel:
    def test_km_without_censoring_is_the_empirical_pmf(self):
        m = hz.km_fit([2, 2, 4, 4], [])
        assert m.pmf[2] == pytest.approx(0.5) and m.pmf[4] == pytest.approx(0.5)
        assert m.tail == pytest.approx(0.0)

    def test_censoring_moves_mass_to_the_right(self):
        # One product still waiting after 10 weeks: its gap is > 10, not unknown.
        m = hz.km_fit([2, 2, 4], [10])
        assert m.pmf[2] == pytest.approx(0.5)           # 2 of 4 at risk
        assert m.pmf[4] == pytest.approx(0.5 * 1 / 2)   # 1 of 2 at risk
        assert m.tail == pytest.approx(0.25)            # the censored one survives

    def test_posterior_shrinks_toward_the_prior(self):
        prior = hz.km_fit([2, 2, 2, 2], [])
        post = hz.posterior([6, 6], prior, alpha=2)
        assert post.pmf[6] == pytest.approx(0.5) and post.pmf[2] == pytest.approx(0.5)

    def test_conditioning_on_weeks_elapsed(self):
        m = hz.GapModel(tuple([0, 0, 0.5, 0, 0.25] + [0] * 48), 0.25)
        probs = hz.next_week_probs(m, elapsed=2, ongoing=False, horizon=4)
        # Gap 2 is already ruled out: next start at gap 4 (k=2) carries 0.25 / 0.5.
        assert probs == pytest.approx([0.0, 0.5, 0.0, 0.0])
        assert sum(hz.next_week_probs(m, elapsed=0, ongoing=True, horizon=4)) == pytest.approx(0.75)

    def test_best_window_prefers_narrow_when_it_keeps_the_mass(self):
        assert hz.best_window([0.05, 0.6, 0.05, 0.0, 0.0]) == (2, 1, pytest.approx(0.6))
        # A weak 3-week window (0.39 < 0.5) widens when week 4 adds >= 0.12 ...
        _k, width, mass = hz.best_window([0.13] * 6)
        assert width == 4 and mass == pytest.approx(0.52)
        # ... but not for a smaller gain.
        assert hz.best_window([0.1] * 6)[1] == 3

    def test_predict_uses_run_starts_up_to_t(self):
        prior = hz.km_fit([2] * 20, [])
        p = hz.predict([10, 12, 14, 30], t=15, half_now=False, prior=prior)
        assert p.last_start == 14 and p.gaps_n == 2 and p.first_week == 16
        assert hz.predict([10], t=10 + hz.STALE_AFTER_WEEKS + 1, half_now=False, prior=prior) is None


class TestGate:
    def test_report_passes_only_when_every_criterion_holds(self):
        def case(t, raw, hit, naive, b0):
            return hz.Case("p", "coles", t, raw, hit, 2, naive, b0, 3, 0.5, hit)
        calib = [case(1, 0.8, i < 8, False, False) for i in range(10)]
        hold = [case(2, 0.8, i < 8, i < 3, i < 4) for i in range(10)]
        rep = hz.gate_report(calib + hold, {1}, {2})
        gate = rep["retailers"]["coles"]["gate"]
        assert gate["noninferior_to_b0_at_equal_width"] and gate["beats_naive_same_width"]
        assert gate["ece_le_0_05"]


def test_track_records_count_non_overlapping_calls():
    # A 2-week window shown at t=0 covers weeks 1-2: the re-shows at t=1, 2 are
    # the same call; the next independent call is at t=3.
    cs = [hz.Case("p", "coles", t, 0.5, t != 3, 2, False, None, None, 0.3, None, t + 1)
          for t in range(6)]
    assert hz.product_track_records(cs)["p"] == (2, 1, 2, 1)
