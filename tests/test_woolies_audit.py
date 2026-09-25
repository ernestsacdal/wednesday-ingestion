"""audit_woolies.score — served set vs a complete live Half Price node (pure)."""
from src.audit_woolies import score
from src.scrapers.woolies_specials import to_truth
from src.truth import TruthRow


def _t(sku, half=True, sale=250, available=True):
    return TruthRow(retailer_sku=sku, is_half=half, sale_cents=sale, was_cents=500,
                    available=available)


def test_perfect_live_week():
    truth = {s: _t(s) for s in ("w:1", "w:2")}
    served = {"w:1": (True, 250), "w:2": (True, 250)}
    sc = score(served, truth, stocked={"w:1", "w:2"})
    assert (sc.precision_pct, sc.recall_pct, sc.price_exact_pct) == (100.0, 100.0, 100.0)


def test_dump_fallback_errors_are_counted():
    truth = {"w:1": _t("w:1"), "w:2": _t("w:2"), "w:3": _t("w:3"),
             "w:9": _t("w:9", half=False, sale=None, available=False)}
    served = {
        "w:1": (True, 250),      # correct
        "w:2": (True, 300),      # right item, wrong price
        "w:9": (True, 250),      # false positive: node says unavailable, not half
        "w:8": (True, 250),      # false positive: not on the node at all
        "w:3": (False, 400),     # missed (served as a sub-half special)
    }
    sc = score(served, truth, stocked={"w:1", "w:2", "w:3", "w:8", "w:9"})
    assert sc.served_half == 4 and sc.false_pos == ["w:8", "w:9"]
    assert sc.fp_unavailable == 1
    assert sc.missed == ["w:3"]
    assert sc.precision_pct == 50.0
    assert sc.recall_pct == round(100 * 2 / 3, 1)   # w:1, w:2 of w:1..w:3
    assert sc.price_exact_pct == 50.0


def test_deals_missing_from_our_catalogue_still_count_against_recall():
    truth = {"w:1": _t("w:1"), "w:new": _t("w:new")}
    sc = score({"w:1": (True, 250)}, truth, stocked={"w:1"})
    assert sc.recall_pct == 50.0 and sc.not_stocked == 1 and sc.missed == []


def test_to_truth_keeps_unavailable_tiles():
    row = to_truth({"Stockcode": 42, "IsHalfPrice": False, "Price": None, "WasPrice": 5,
                    "IsAvailable": False, "InstoreIsOnSpecial": False})
    assert row == TruthRow("woolworths:42", False, None, 500, False, False)
    assert to_truth({"Name": "no code"}) is None
