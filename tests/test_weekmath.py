"""Most-recent-Wednesday week math — shared by the writers, the send_alerts
week-gate and verify_data. An off-by-one here would silently skip a digest or
serve a stale week, so pin every weekday."""
from datetime import date, timedelta

from src.send_alerts import most_recent_wednesday

# 2026-06-17 is a Wednesday (the live current week_start).
WED = date(2026, 6, 17)


def test_wednesday_maps_to_itself():
    assert most_recent_wednesday(WED) == WED


def test_every_day_of_the_week_resolves_to_that_wednesday():
    # Wed .. the following Tue all belong to the same promo week.
    for offset in range(7):
        assert most_recent_wednesday(WED + timedelta(days=offset)) == WED


def test_next_wednesday_rolls_over():
    assert most_recent_wednesday(WED + timedelta(days=7)) == WED + timedelta(days=7)


def test_day_before_belongs_to_prior_week():
    assert most_recent_wednesday(WED - timedelta(days=1)) == WED - timedelta(days=7)
