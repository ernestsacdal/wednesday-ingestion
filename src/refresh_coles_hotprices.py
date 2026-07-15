"""Replace this week's Coles half-price specials with authoritative hotprices data.

Coles' half-price list is derived from the public hotprices.org daily dump
(see src/scrapers/hotprices.py) and written with source='hotprices', then any
leftover current-week Coles specials still sourced from StockUp (the old, fragile
self-scrape) are deleted.

Order matters for safety (mirrors refresh_woolies_specials):
  1. write authoritative rows first (so a failure never leaves Coles empty),
  2. then delete the remaining stockup-sourced Coles specials for the week.
Name-matching products get their special overwritten in step 1 (source flips to
hotprices); the rest are removed in step 2.

Run locally:
    python -m src.refresh_coles_hotprices --verbose
Wired into the weekly pipeline. Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from src.db.bulk_writer import bulk_write_to_db
from src.db.reader import max_week_start
from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.scrapers.hotprices import scrape

# Creating a NEW week requires this many items whose newest price change is
# dated on/after the new week_start. A genuine promo-rollover Wednesday dump
# shows ~1,000-2,500 (2026-07-15's real dump: 1,070 currently-half alone); a
# dump built BEFORE the rollover shows ~0 — its entries cannot carry the new
# Wednesday's date. 200 leaves wide margin on both sides.
_MIN_FRESH_ITEMS_FOR_NEW_WEEK = 200


def refresh_coles(*, db_url: str, log: logging.Logger) -> int:
    """Returns the number of authoritative Coles specials written."""
    out = scrape(log)
    if not out.specials:
        # Distinct from a successful "wrote 0 rows": the dump returned nothing
        # usable (likely a fetch error or upstream outage), so we deliberately
        # keep last run's rows rather than wipe Coles. Surfaced loudly.
        log.error("refresh_coles.no_data — hotprices dump yielded 0 specials; KEEPING existing "
                  "rows (no write, no delete). If this persists for 2+ weeks, investigate the dump.")
        return 0

    week_start = out.specials[0].week_start

    # New-week freshness gate (ADR-0001): only CREATE a new week when the dump
    # demonstrably contains it — a stale (pre-Wednesday) dump would mislabel
    # last week's prices as the new week. Same-week rewrites are never gated.
    db_max = max_week_start(db_url)
    fresh = out.run.fresh_week_items or 0
    if (db_max is None or week_start > db_max) and fresh < _MIN_FRESH_ITEMS_FOR_NEW_WEEK:
        log.error(
            "refresh_coles.skip stale_dump_for_new_week fresh_items=%d (<%d) — dump likely "
            "predates week=%s; KEEPING existing rows (no write, no delete)",
            fresh, _MIN_FRESH_ITEMS_FOR_NEW_WEEK, week_start,
        )
        return 0

    # 1. Write authoritative Coles half-price rows. write_to_db is
    # transaction-safe (all-or-nothing); if it raises, we have NOT deleted any
    # StockUp rows yet, so worst case is stale-but-present data + a clear log.
    try:
        result = bulk_write_to_db(out, db_url=db_url, log=log, sync_week=True)
    except Exception:
        log.exception("refresh_coles.write_failed — StockUp Coles rows left in place "
                      "(no delete attempted); safe to retry")
        raise

    # 2. Remove any remaining current-week Coles specials still from StockUp.
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                delete from specials s
                using products p
                where s.product_id = p.id
                  and p.retailer = 'coles'
                  and s.week_start = %(w)s
                  and s.source in ('stockup_post', 'stockup_sheet')
                """,
                {"w": week_start},
            )
            deleted = cur.rowcount
        conn.commit()

    log.info(
        "refresh_coles.done written=%d deleted_stockup=%d week_start=%s",
        result.specials_written, deleted, week_start,
    )
    return result.specials_written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_coles_hotprices")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    written = refresh_coles(db_url=db_url, log=log)
    return 0 if written > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
