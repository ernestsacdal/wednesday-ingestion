"""Supabase reader: loads specials + products into WeeklySpecial dataclasses.

Used by the statistical predictor when running against the live DB instead of
a one-shot JSON dump from a single scrape. Pulling from `specials` gives the
predictor multi-week price history per product — required for emitting
predictions with non-trivial cycle_count.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import psycopg

from src.models import WeeklySpecial


def max_week_start(db_url: str) -> date | None:
    """The newest promo week in specials — what the app treats as "this week".

    Shared by the refresh guards (ADR-0001): a write whose week_start exceeds
    this value would CREATE a new week, which is only allowed when the week
    arrives whole (both retailers) and fresh (dump contains the new Wednesday).
    """
    with psycopg.connect(db_url, connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute("select max(week_start) from specials")
        return cur.fetchone()[0]


def load_specials_from_db(
    db_url: str,
    log: logging.Logger,
) -> list[WeeklySpecial]:
    """Pull every specials row joined with its product. One WeeklySpecial per row.

    The `last_halfprice_*` fields from the original scrape are NOT stored on
    specials (they're WeeklySpecial-only metadata, not in the schema), so they
    come back as None / empty here. The predictor's cycle computation now
    relies on the chronological sequence of week_start values per product
    instead — which is more reliable across heterogeneous post formats.
    """
    sql = """
        select
            p.retailer,
            p.name,
            coalesce(p.category, 'Uncategorised') as category,
            s.regular_price_cents,
            s.sale_price_cents,
            s.discount_pct,
            s.is_half_price,
            s.week_start,
            s.week_end,
            s.source
        from specials s
        join products p on p.id = s.product_id
        order by p.retailer, p.name, s.week_start
    """
    specials: list[WeeklySpecial] = []
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    for (
        retailer, name, category, regular_cents, sale_cents,
        discount_pct, is_half, week_start, week_end, source,
    ) in rows:
        specials.append(WeeklySpecial(
            retailer=retailer,
            product_name=name,
            category=category,
            regular_price_cents=regular_cents,
            sale_price_cents=sale_cents,
            discount_pct=discount_pct,
            is_half_price=is_half,
            # last_halfprice_* not persisted; predictor will fall back to
            # deriving intervals from the week_start sequence per product.
            last_halfprice_raw="",
            last_halfprice_weeks_ago=None,
            last_halfprice_retailer=None,
            week_start=week_start if isinstance(week_start, date) else date.fromisoformat(str(week_start)),
            week_end=week_end if isinstance(week_end, date) else date.fromisoformat(str(week_end)),
            source=source,
            source_url="",
            scraped_at=datetime.now(timezone.utc),
        ))

    log.info("predict.db.loaded specials=%d", len(specials))
    return specials
