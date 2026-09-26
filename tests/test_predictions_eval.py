"""As-shown prediction scoring (src/eval/predictions_eval.py) — pure, no DB."""
from datetime import date, datetime, timezone

from src.eval import predictions_eval as pe

UTC = timezone.utc
W = date(2026, 7, 29)          # a Wednesday (promo week start)


def _pred(ws, we, conf=0.8, at=datetime(2026, 7, 26, 8, 0, tzinfo=UTC)):
    return pe.Pred("p1", ws, we, conf, "statistical", at)


def _claim(ws, we, week=W, conf=0.8):
    return ("p1", week, ws, we, conf, "statistical", datetime(2026, 7, 26, tzinfo=UTC))


class TestLedger:
    def test_in_effect_is_the_latest_computed_before_the_cutoff(self):
        early = _pred(date(2026, 8, 5), date(2026, 8, 18), at=datetime(2026, 7, 19, tzinfo=UTC))
        late = _pred(date(2026, 8, 12), date(2026, 8, 25), at=datetime(2026, 7, 26, tzinfo=UTC))
        # Computed after Wed 29 Jul 13:30 Sydney -> not what users saw that week.
        after = _pred(date(2026, 9, 2), date(2026, 9, 15), at=datetime(2026, 7, 29, 5, 0, tzinfo=UTC))
        assert pe.in_effect([early, late, after], pe.shown_cutoff(W)) is late

    def test_expired_and_already_half_claims_are_not_recorded(self):
        active = _pred(date(2026, 8, 5), date(2026, 8, 18))
        expired = _pred(date(2026, 7, 1), date(2026, 7, 14))
        rows = pe.ledger_rows({"p1": [active]}, {"p1": set()}, [W])
        assert len(rows) == 1
        assert pe.ledger_rows({"p1": [expired]}, {}, [W]) == []
        assert pe.ledger_rows({"p1": [active]}, {"p1": {W}}, [W]) == []   # half today

    def test_each_prediction_is_scored_once_from_its_first_week(self):
        at = datetime(2026, 7, 26, tzinfo=UTC)
        rows = [("p1", date(2026, 8, 5), 0, 0, 0.8, "m", at),
                ("p1", W, 0, 0, 0.8, "m", at)]
        assert [r[1] for r in pe.first_shown_claims(rows)] == [W]


class TestScoreClaim:
    CURRENT = date(2026, 9, 23)

    def test_hit_when_a_half_week_overlaps_the_remaining_window(self):
        # Window Sat 8 Aug - Fri 21 Aug overlaps promo weeks 5 Aug, 12 Aug, 19 Aug.
        res = pe.score_claim(_claim(date(2026, 8, 8), date(2026, 8, 21)), {date(2026, 8, 19)},
                             self.CURRENT)
        assert res == (True, 3, True)    # naive = the 3 weeks after W: 5, 12, 19 Aug -> hit

    def test_miss_and_naive_hit(self):
        res = pe.score_claim(_claim(date(2026, 8, 12), date(2026, 8, 25)), {date(2026, 8, 5)},
                             self.CURRENT)
        assert res == (False, 2, True)   # naive = 5, 12 Aug -> the 5 Aug sale

    def test_window_that_started_before_the_shown_week_is_cut_to_what_remains(self):
        # Window began 15 Jul but was first shown 29 Jul: only 29 Jul.. counts.
        res = pe.score_claim(_claim(date(2026, 7, 15), date(2026, 8, 4)), {date(2026, 7, 22)},
                             self.CURRENT)
        assert res == (False, 1, False)   # naive = 5 Aug only

    def test_windows_not_yet_over_are_not_scored(self):
        assert pe.score_claim(_claim(date(2026, 9, 16), date(2026, 9, 29)), set(),
                              self.CURRENT) is None


class TestSummaries:
    def _s(self, conf, hit, width=3, naive=False):
        return pe.Scored("p", "coles", W, conf, "statistical", hit, width, naive)

    def test_hit_rate_width_naive_brier_and_ece(self):
        scored = [self._s(0.8, True, 2, True), self._s(0.8, False, 4),
                  self._s(0.3, True, 3), self._s(0.3, False, 3)]
        s = pe.summarize(scored)
        assert s["n"] == 4 and s["hit_rate"] == 0.5 and s["naive_hit_rate"] == 0.25
        assert s["mean_width_weeks"] == 3.0
        # Brier: (0.8-1)^2 + 0.8^2 + (0.3-1)^2 + 0.3^2 = 0.04+0.64+0.49+0.09 = 1.26 / 4
        assert s["brier"] == 0.315
        # ECE: bins 0.8 (acc .5, conf .8) and 0.3 (acc .5, conf .3): .5*.3 + .5*.2
        assert s["ece"] == 0.25

    def test_app_buckets_match_verdict_thresholds(self):
        assert pe.app_bucket(0.70) == "high" and pe.app_bucket(0.69) == "medium"
        assert pe.app_bucket(0.50) == "medium" and pe.app_bucket(0.30) == "low"
        assert pe.app_bucket(0.29) is None
