"""Weekly half-price history per product, rebuilt from the hotprices price history.

Prediction labels used to come from the specials table, which mixes one-off
backfilled change-point rows (not Wednesday-aligned, one per sale run) with
live weekly rows (one per promo week) — and some live Woolworths weeks were
overwritten with the following week's set by the old UTC week bug. This
module derives ONE consistent label for every product and promo week from
the dump's full price history (back to 2023), using the same rule the
production derivation uses for a half-price event: a price change at least
48% below the price immediately before it.

A sale run starts in the promo week of that change and lasts until the next
price change (capped at MAX_RUN_WEEKS — an item "sitting" at half price for
months is a stale or unavailable listing, not a months-long promotion).
Complete live Woolworths crawls (truth_snapshots 'woolies_api') override the
derived label for their weeks.

Weeks are integer indices of Sydney promo weeks (Wednesday starts) from EPOCH.
"""
from __future__ import annotations

from datetime import date, timedelta

from src.weeks import promo_week_start

EPOCH = date(2023, 1, 4)          # a Wednesday
EVENT_MIN_OFF = 0.48              # same as hotprices._EVENT_MIN_OFF
MAX_RUN_WEEKS = 3


def week_idx(d: date) -> int:
    return (promo_week_start(d) - EPOCH).days // 7


def week_date(i: int) -> date:
    return EPOCH + timedelta(weeks=i)


def half_weeks(history: list[tuple[date, int]], *, today_week: int) -> set[int]:
    """Promo-week indices a product was half-price, from its change points."""
    pts = sorted(history)                     # oldest first
    weeks: set[int] = set()
    for i in range(1, len(pts)):
        on, now_c = pts[i]
        prev_c = pts[i - 1][1]
        if prev_c <= 0 or now_c <= 0 or (1 - now_c / prev_c) < EVENT_MIN_OFF:
            continue
        start = week_idx(on)
        last = start + MAX_RUN_WEEKS - 1
        if i + 1 < len(pts):                  # the price changed again: run ended
            last = min(last, week_idx(pts[i + 1][0] - timedelta(days=1)))
        weeks.update(range(start, min(last, today_week) + 1))
    return weeks


def run_starts(weeks: set[int]) -> list[int]:
    """First week of each run of consecutive half-price weeks, ascending."""
    return sorted(w for w in weeks if w - 1 not in weeks)


def overlay_truth(series: dict[str, set[int]], week: int, truth_half: set[str],
                  products: set[str]) -> None:
    """A complete authoritative crawl for ``week`` decides that week for ``products``."""
    for pid in products:
        s = series.setdefault(pid, set())
        if pid in truth_half:
            s.add(week)
        else:
            s.discard(week)
