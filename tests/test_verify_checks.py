"""verify_data aggregation: severity handling, the WOOLIES_LIVE_REQUIRED escape
hatch, and the live-Woolies deadline gating. Pure — no DB."""
from datetime import datetime, timezone

from src import verify_data as vd
from src.weeks import live_deadline_passed


def _check(name, *, severity="fail"):
    return vd.Check(name, "select 1", lambda v: v >= 1, ">= 1", severity=severity)


def test_passing_checks_report_nothing():
    failed, warned = vd.evaluate([(_check("a"), 5), (_check("b", severity="warn"), 1)], env={})
    assert failed == [] and warned == []


def test_fail_and_warn_are_separated():
    results = [(_check("hard"), 0), (_check("soft", severity="warn"), 0)]
    failed, warned = vd.evaluate(results, env={})
    assert failed == ["hard"]
    assert warned == ["soft"]


def test_live_woolies_checks_fail_by_default():
    live = _check("woolies_live_heartbeat")
    failed, _ = vd.evaluate([(live, 0)], env={})
    assert failed == ["woolies_live_heartbeat"]


def test_escape_hatch_downgrades_only_live_checks():
    live = _check("woolies_live_heartbeat")
    other = _check("coles_current_half")
    failed, warned = vd.evaluate([(live, 0), (other, 0)], env={"WOOLIES_LIVE_REQUIRED": "0"})
    assert failed == ["coles_current_half"]
    assert warned == ["woolies_live_heartbeat"]


def test_live_checks_are_registered():
    names = {c.name for c in vd.CHECKS}
    assert vd.LIVE_WOOLIES_CHECKS <= names


def test_current_week_live_is_deadline_gated():
    check = next(c for c in vd.CHECKS if c.name == "woolies_current_week_live")
    assert check.active is live_deadline_passed
    # Wed 30 Sep 12:30 AEST: the midday roll may still be running -> inactive.
    assert not check.active(datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc))
    # Wed 30 Sep 15:07 AEST (the verify-live cron) -> enforced.
    assert check.active(datetime(2026, 9, 30, 5, 7, tzinfo=timezone.utc))


def _named(name):
    return next(c for c in vd.CHECKS if c.name == name)


def test_chemist_warehouse_guards_fail_the_run():
    for name in ("cw_hidden_from_default_search", "cw_not_in_specials", "cw_not_in_aliases"):
        assert _named(name).severity == "fail"


def test_cw_search_guard_calls_rpcs_like_installed_apps():
    # Installed apps never send `stores`; the guard must call the RPCs the same way.
    sql = _named("cw_hidden_from_default_search").sql
    assert "stores" not in sql
    for rpc in ("search_products_by_name(", "browse_category(", "browse_category_group("):
        assert rpc in sql


def test_cw_catalogue_pending_is_not_a_warning():
    check = _named("cw_catalogue_has_deals")
    assert check.severity == "warn"
    assert check.ok(-1) and check.ok(230)
    assert not check.ok(0)


def test_cw_usual_coverage_is_info_only():
    check = _named("cw_usual_coverage")
    assert all(check.ok(v) for v in (None, 0, 100))


def test_catalogue_freshness_counts_coles_and_woolworths_only():
    sql = _named("catalogue_recently_refreshed").sql
    assert "retailer in ('coles', 'woolworths')" in sql
