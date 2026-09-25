"""Sydney-local promo-week math (src/weeks.py). The UTC date is still Tuesday
until 10:00 AEST / 11:00 AEDT on a Wednesday — every case where that
difference matters is pinned here, across both daylight-saving boundaries."""
from datetime import date, datetime, timezone

import pytest

from src.weeks import (
    current_promo_week,
    live_deadline_passed,
    promo_week_start,
    sydney_today,
)

UTC = timezone.utc


def _utc(*args):
    return datetime(*args, tzinfo=UTC)


class TestPromoWeekStart:
    def test_every_weekday_maps_to_its_wednesday(self):
        wed = date(2026, 9, 23)
        for offset in range(7):
            assert promo_week_start(date(2026, 9, 23 + offset)) == wed

    def test_tuesday_belongs_to_the_previous_week(self):
        assert promo_week_start(date(2026, 9, 22)) == date(2026, 9, 16)


class TestCurrentPromoWeek:
    def test_wednesday_morning_aest_is_the_new_week(self):
        # Wed 30 Sep 06:00 AEST == Tue 29 Sep 20:00 UTC.
        assert current_promo_week(_utc(2026, 9, 29, 20, 0)) == date(2026, 9, 30)

    def test_tuesday_evening_aest_is_still_last_week(self):
        # Tue 29 Sep 23:59 AEST == Tue 29 Sep 13:59 UTC.
        assert current_promo_week(_utc(2026, 9, 29, 13, 59)) == date(2026, 9, 23)

    def test_aedt_midnight_boundary(self):
        # After DST starts (4 Oct 2026) Sydney is UTC+11: Wed 7 Oct 00:30 AEDT
        # is Tue 6 Oct 13:30 UTC.
        assert current_promo_week(_utc(2026, 10, 6, 13, 30)) == date(2026, 10, 7)
        assert current_promo_week(_utc(2026, 10, 6, 12, 59)) == date(2026, 9, 30)

    def test_dst_end_boundary(self):
        # DST ends 5 Apr 2027; Wed 7 Apr 2027 00:30 AEST is Tue 6 Apr 14:30 UTC.
        assert current_promo_week(_utc(2027, 4, 6, 14, 30)) == date(2027, 4, 7)

    def test_sydney_today_rejects_naive_datetimes(self):
        with pytest.raises(ValueError):
            sydney_today(datetime(2026, 9, 30, 6, 0))


class TestLiveDeadline:
    def test_before_wednesday_1500_is_not_passed(self):
        # Wed 30 Sep 14:59 AEST == 04:59 UTC.
        assert not live_deadline_passed(_utc(2026, 9, 30, 4, 59))

    def test_wednesday_1500_passes(self):
        assert live_deadline_passed(_utc(2026, 9, 30, 5, 0))

    def test_rest_of_week_is_passed(self):
        # Thu 1 Oct 01:00 AEST.
        assert live_deadline_passed(_utc(2026, 9, 30, 15, 0))
        # Tue 6 Oct 23:00 AEDT.
        assert live_deadline_passed(_utc(2026, 10, 6, 12, 0))

    def test_aedt_deadline_shift(self):
        # Wed 7 Oct is AEDT (UTC+11): 15:00 local == 04:00 UTC.
        assert not live_deadline_passed(_utc(2026, 10, 7, 3, 59))
        assert live_deadline_passed(_utc(2026, 10, 7, 4, 0))
