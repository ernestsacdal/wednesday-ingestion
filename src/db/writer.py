"""Supabase writer: maps a ScrapeOutput onto 4 tables.

Tables touched per pipeline run:
  scrape_runs        — 1 audit row per invocation, status finalised at end
  products           — upserted by (retailer, synthetic_sku) where synthetic_sku
                       is built from a normalised product name. StockUp posts
                       don't expose real retailer SKUs; future direct-scrape
                       sources should populate retailer_sku with the real value
                       and skip the synthesis path.
  price_observations — one row per WeeklySpecial (current week's sale price)
  specials           — one row per WeeklySpecial, dedup'd by (product_id, week_start)

The connection is established via SUPABASE_DB_URL. The session pooler
(port 5432 on aws-N-REGION.pooler.supabase.com) is the right target on the
free tier when running from an IPv4-only network — the direct connection
(db.REF.supabase.co) is IPv6-only.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

import psycopg

from src.models import Prediction, ScrapeOutput, WeeklySpecial


_NAME_NORMALISE = re.compile(r"\s+")


def _synthetic_sku(retailer: str, name: str) -> str:
    """Stable per-(retailer, name) key when no real SKU is available."""
    normalised = _NAME_NORMALISE.sub(" ", name.strip()).lower()
    return f"stockup:{normalised}"


@dataclass
class WriteResult:
    scrape_run_id: str
    products_upserted: int
    observations_written: int
    specials_written: int


def write_to_db(
    output: ScrapeOutput,
    *,
    db_url: str,
    log: logging.Logger,
) -> WriteResult:
    """Push the scrape output to Supabase. Returns counts for logging.

    Strategy: one connection, autocommit off; each special row goes through
    a single round-trip insert sequence. For ~180 rows this is well under a
    second and keeps the transaction boundary tight. The scrape_run audit row
    is inserted first so even partial failures leave a trace.
    """
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            scrape_run_id = _insert_scrape_run(cur, output)
            products_upserted = 0
            observations_written = 0
            specials_written = 0

            for special in output.specials:
                product_id = _upsert_product(cur, special)
                products_upserted += 1
                if _insert_observation(cur, product_id, special):
                    observations_written += 1
                if _upsert_special(cur, product_id, special):
                    specials_written += 1

            _finalise_scrape_run(
                cur,
                scrape_run_id,
                output,
                items_written=specials_written,
            )
        conn.commit()

    log.info(
        "db.write.done run_id=%s products=%d observations=%d specials=%d",
        scrape_run_id,
        products_upserted,
        observations_written,
        specials_written,
    )
    return WriteResult(
        scrape_run_id=scrape_run_id,
        products_upserted=products_upserted,
        observations_written=observations_written,
        specials_written=specials_written,
    )


def _insert_scrape_run(cur: psycopg.Cursor, output: ScrapeOutput) -> str:
    cur.execute(
        """
        insert into scrape_runs (source, run_at, status, items_found, duration_ms, error, notes)
        values (%(source)s, %(run_at)s, %(status)s, %(items_found)s, %(duration_ms)s, %(error)s, %(notes)s)
        returning id::text
        """,
        {
            "source": output.run.source,
            "run_at": output.run.started_at,
            "status": output.run.status,
            "items_found": output.run.items_found,
            "duration_ms": output.run.duration_ms,
            "error": output.run.error,
            "notes": output.run.notes or output.run.source_url,
        },
    )
    return cur.fetchone()[0]


def _finalise_scrape_run(
    cur: psycopg.Cursor,
    scrape_run_id: str,
    output: ScrapeOutput,
    *,
    items_written: int,
) -> None:
    """Reflect the final items_written count so the audit row is accurate."""
    cur.execute(
        """
        update scrape_runs
           set items_found = %(items_written)s,
               status = %(status)s
         where id = %(id)s::uuid
        """,
        {
            "id": scrape_run_id,
            "items_written": items_written,
            "status": output.run.status,
        },
    )


def _upsert_product(cur: psycopg.Cursor, special: WeeklySpecial) -> str:
    sku = _synthetic_sku(special.retailer, special.product_name)
    cur.execute(
        """
        insert into products
            (retailer, retailer_sku, name, category, regular_price_cents, image_url,
             image_fetched_at, last_seen)
        values (%(retailer)s, %(sku)s, %(name)s, %(category)s, %(regular_price_cents)s,
                %(image_url)s,
                case when %(image_url)s is not null then now() else null end, now())
        on conflict (retailer, retailer_sku) do update
          set name = excluded.name,
              category = excluded.category,
              regular_price_cents = greatest(products.regular_price_cents, excluded.regular_price_cents),
              -- Keep any existing image; only fill from the source when we have
              -- one and the row doesn't already.
              image_url = coalesce(products.image_url, excluded.image_url),
              image_fetched_at = case
                when products.image_url is null and excluded.image_url is not null then now()
                else products.image_fetched_at
              end,
              last_seen = now()
        returning id::text
        """,
        {
            "retailer": special.retailer,
            "sku": sku,
            "name": special.product_name,
            "category": special.category,
            "regular_price_cents": special.regular_price_cents,
            "image_url": special.image_url,
        },
    )
    return cur.fetchone()[0]


def _insert_observation(
    cur: psycopg.Cursor,
    product_id: str,
    special: WeeklySpecial,
) -> bool:
    """Upsert one price_observation row for the current week's sale price.

    Idempotent on (product_id, observed_at, source) — the unique constraint
    added in migration 0013 — so re-running the pipeline within a week updates
    the row instead of duplicating it.
    """
    cur.execute(
        """
        insert into price_observations
            (product_id, price_cents, is_special, discount_pct, observed_at, source)
        values (%(product_id)s::uuid, %(price_cents)s, %(is_special)s, %(discount_pct)s, %(observed_at)s, %(source)s)
        on conflict (product_id, observed_at, source) do update
          set price_cents = excluded.price_cents,
              is_special = excluded.is_special,
              discount_pct = excluded.discount_pct
        """,
        {
            "product_id": product_id,
            "price_cents": special.sale_price_cents,
            "is_special": True,
            "discount_pct": special.discount_pct,
            "observed_at": special.week_start,
            "source": special.source,
        },
    )
    return cur.rowcount == 1


def _upsert_special(
    cur: psycopg.Cursor,
    product_id: str,
    special: WeeklySpecial,
) -> bool:
    """Upsert one specials row keyed by (product_id, week_start).

    Re-running for the same week updates the sale price + discount instead of
    creating a duplicate, so the cron is idempotent within a week.
    """
    cur.execute(
        """
        insert into specials
            (product_id, week_start, week_end, regular_price_cents, sale_price_cents,
             discount_pct, is_half_price, source)
        values (%(product_id)s::uuid, %(week_start)s, %(week_end)s,
                %(regular_price_cents)s, %(sale_price_cents)s, %(discount_pct)s,
                %(is_half_price)s, %(source)s)
        on conflict (product_id, week_start) do update
          set sale_price_cents = excluded.sale_price_cents,
              regular_price_cents = excluded.regular_price_cents,
              discount_pct = excluded.discount_pct,
              is_half_price = excluded.is_half_price,
              week_end = excluded.week_end
        """,
        {
            "product_id": product_id,
            "week_start": special.week_start,
            "week_end": special.week_end,
            "regular_price_cents": special.regular_price_cents,
            "sale_price_cents": special.sale_price_cents,
            "discount_pct": special.discount_pct,
            "is_half_price": special.is_half_price,
            "source": special.source,
        },
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Predictions writer
# ---------------------------------------------------------------------------

@dataclass
class PredictionsWriteResult:
    inserted: int
    skipped_unmatched: int  # predictions whose product wasn't in DB (shouldn't happen)


def write_predictions_to_db(
    predictions: list[Prediction],
    *,
    db_url: str,
    log: logging.Logger,
) -> PredictionsWriteResult:
    """Insert prediction rows into the predictions table.

    Each prediction is looked up by (retailer, name) to find product_id, then
    inserted. Unique (product_id, computed_at) prevents duplicates within the
    same run; cross-run dedup is implicit because computed_at differs.
    """
    inserted = 0
    skipped_unmatched = 0

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            # Build a (retailer, name) -> product_id lookup once.
            cur.execute("select id::text, retailer, name from products")
            lookup: dict[tuple[str, str], str] = {
                (retailer, name): pid for pid, retailer, name in cur.fetchall()
            }

            for p in predictions:
                key = (p.retailer, p.product_name.strip())
                product_id = lookup.get(key)
                if product_id is None:
                    skipped_unmatched += 1
                    continue
                cur.execute(
                    """
                    insert into predictions
                        (product_id, predicted_window_start, predicted_window_end,
                         confidence, confidence_tier, method, mean_interval_weeks, stddev_weeks,
                         cycle_count, computed_at)
                    values (%(product_id)s::uuid, %(ws)s, %(we)s,
                            %(confidence)s, %(confidence_tier)s, %(method)s, %(mean)s, %(stddev)s,
                            %(cycle_count)s, %(computed_at)s)
                    on conflict (product_id, computed_at) do nothing
                    """,
                    {
                        "product_id": product_id,
                        "ws": p.predicted_window_start,
                        "we": p.predicted_window_end,
                        "confidence": p.confidence,
                        "confidence_tier": p.confidence_tier,
                        "method": p.method,
                        "mean": p.mean_interval_weeks,
                        "stddev": p.stddev_weeks,
                        "cycle_count": p.cycle_count,
                        "computed_at": p.computed_at,
                    },
                )
                if cur.rowcount == 1:
                    inserted += 1
        conn.commit()

    log.info(
        "predict.db.written inserted=%d skipped_unmatched=%d",
        inserted, skipped_unmatched,
    )
    return PredictionsWriteResult(inserted=inserted, skipped_unmatched=skipped_unmatched)
