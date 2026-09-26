"""PAV calibration (src/eval/calibrate.py) and its use by the predictor — pure."""
from datetime import date, timedelta

from src.eval import calibrate
from src.models import WeeklySpecial
from src.prediction import statistical


class TestPav:
    def test_fit_is_monotone_even_when_raw_rates_are_not(self):
        # Observed: 0.3 -> 60% hits, 0.5 -> 40%, 0.8 -> 55% (the live pattern).
        pairs = ([(0.3, i < 6) for i in range(10)] + [(0.5, i < 4) for i in range(10)]
                 + [(0.8, i < 55) for i in range(100)])
        blocks = calibrate.pav_fit(pairs, min_block=1)
        values = [b.value for b in blocks]
        assert values == sorted(values)
        assert blocks[0].value == 0.5          # 0.3 and 0.5 pooled: 10/20

    def test_small_blocks_are_merged(self):
        pairs = [(0.1, False)] * 5 + [(0.9, True)] * 5
        blocks = calibrate.pav_fit(pairs, min_block=8)
        assert len(blocks) == 1 and blocks[0].value == 0.5 and blocks[0].n == 10

    def test_apply_steps_and_clamps(self):
        blocks = [calibrate.Block(0.4, 0.01, 300), calibrate.Block(0.9, 0.99, 300)]
        assert calibrate.apply(blocks, 0.2) == calibrate.CLAMP_LO
        assert calibrate.apply(blocks, 0.6) == calibrate.CLAMP_HI
        assert calibrate.apply(blocks, 0.99) == calibrate.CLAMP_HI   # beyond the last block
        assert calibrate.apply([], 0.72) == 0.72                    # no map: clamp only

    def test_json_round_trip(self):
        blocks = calibrate.pav_fit([(0.2, False), (0.8, True)], min_block=1)
        assert calibrate.from_json(calibrate.to_json(blocks)) == blocks


def _special(pid, name, week):
    return WeeklySpecial(
        retailer="coles", product_name=name, category="Pantry",
        regular_price_cents=400, sale_price_cents=200, discount_pct=50, is_half_price=True,
        last_halfprice_raw="", last_halfprice_weeks_ago=None, last_halfprice_retailer=None,
        week_start=week, week_end=week + timedelta(days=6), source="hotprices",
        source_url="", scraped_at=None, product_id=pid,
    )


class TestPredictorGrouping:
    TODAY = date(2026, 9, 26)

    def _history(self, pid, name, every):
        start = date(2026, 3, 4)
        return [_special(pid, name, start + timedelta(weeks=every * i)) for i in range(8)]

    def test_same_name_products_get_separate_predictions(self):
        specials = self._history("a", "Tim Tam Original", 3) + self._history("b", "Tim Tam Original", 5)
        preds, _ = statistical.compute_predictions(specials, today=self.TODAY)
        assert sorted(p.product_id for p in preds) == ["a", "b"]
        by_id = {p.product_id: p for p in preds}
        assert by_id["a"].mean_interval_weeks == 3 and by_id["b"].mean_interval_weeks == 5

    def test_calibration_sets_confidence_and_app_tier(self):
        specials = self._history("a", "Kettle Chips", 3)
        cal = {"coles": [calibrate.Block(1.0, 0.52, 500)]}
        [pred], _ = statistical.compute_predictions(specials, today=self.TODAY, calibration=cal)
        assert pred.confidence == 0.52 and pred.confidence_tier == "medium"
        assert pred.raw_confidence is not None and pred.raw_confidence > pred.confidence

    def test_tier_for_uses_the_app_thresholds(self):
        assert statistical.tier_for(0.70) == "high"
        assert statistical.tier_for(0.69) == "medium"
        assert statistical.tier_for(0.30) == "low"
        assert statistical.tier_for(0.29) is None
