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
from pathlib import Path

import psycopg

from src.db.bulk_writer import bulk_write_to_db
from src.scrapers.base import configure_logging
from src.scrapers.hotprices import scrape


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


def _load_dotenv() -> None:
    for env_path in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_coles_hotprices")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        _load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    written = refresh_coles(db_url=db_url, log=log)
    return 0 if written > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
