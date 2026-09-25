"""Promo-week math, in Australia/Sydney local time — the one week convention.

Coles and Woolworths promo weeks run Wednesday to Tuesday in LOCAL time. The
pipeline used to take the runner's UTC date, which is still Tuesday until
10:00 AEST (11:00 AEDT) on a Wednesday. So the 06:00 residential Woolies run
landed in LAST week: it wrote the new promo's live set under the old
week_start and sync_week pruned the old week's real rows (week 2026-09-09 was
overwritten with 2026-09-16's set). Every week computation goes through here
so the scrapers, guards, alerts and verify_data agree on what "this week" is.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")

_WEDNESDAY = 2  # date.weekday(): Monday=0 ... Wednesday=2

# By this local time on a Wednesday the new week should hold the LIVE Woolies
# set (06:00 and 12:30/13:45 residential runs have all had their chance).
LIVE_DEADLINE = time(15, 0)


def sydney_now(now: datetime | None = None) -> datetime:
    """``now`` (tz-aware, default: the current instant) as Sydney local time."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("sydney_now needs a tz-aware datetime")
    return now.astimezone(SYDNEY)


def sydney_today(now: datetime | None = None) -> date:
    return sydney_now(now).date()


def promo_week_start(d: date) -> date:
    """The Wednesday that starts the promo week containing ``d``."""
    return d - timedelta(days=(d.weekday() - _WEDNESDAY) % 7)


def current_promo_week(now: datetime | None = None) -> date:
    return promo_week_start(sydney_today(now))


def live_deadline_passed(now: datetime | None = None) -> bool:
    """True once the current promo week is past Wednesday LIVE_DEADLINE (Sydney)."""
    local = sydney_now(now)
    if local.date() > promo_week_start(local.date()):
        return True
    return local.time() >= LIVE_DEADLINE
