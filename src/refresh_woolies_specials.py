"""Replace this week's Woolworths half-price specials with authoritative data.

Pulls the real Woolworths "Half Price" category via src/scrapers/woolies_specials
and writes it (source=woolies_catalogue), then deletes any leftover current-week
Woolworths specials still sourced from StockUp — which spot-checks proved
unreliable (~half were a smaller discount or not on special).

Order matters for safety:
  1. write authoritative rows first (so a failure never leaves Woolies empty),
  2. then delete the remaining stockup-sourced Woolies specials for the week.
Name-matching products get their special overwritten in step 1 (source flips to
woolies_catalogue); the rest are removed in step 2.

Run locally:
    python -m src.refresh_woolies_specials --verbose
Wire into the weekly pipeline once validated. Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg

from src.db.writer import write_to_db
from src.scrapers.base import configure_logging
from src.scrapers.woolies_specials import build_woolies_session, scrape


def refresh_woolies(*, db_url: str, log: logging.Logger) -> int:
    """Returns the number of authoritative Woolies specials written."""
    out = scrape(build_woolies_session(), log)
    if not out.specials:
        log.error("refresh_woolies.no_data — keeping existing rows, aborting")
        return 0

    week_start = out.specials[0].week_start

    # 1. Write authoritative Woolies half-price rows.
    result = write_to_db(out, db_url=db_url, log=log)

    # 2. Remove any remaining current-week Woolies specials still from StockUp.
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                delete from specials s
                using products p
                where s.product_id = p.id
                  and p.retailer = 'woolworths'
                  and s.week_start = %(w)s
                  and s.source in ('stockup_post', 'stockup_sheet')
                """,
                {"w": week_start},
            )
            deleted = cur.rowcount
        conn.commit()

    log.info(
        "refresh_woolies.done written=%d deleted_stockup=%d week_start=%s",
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
    parser = argparse.ArgumentParser(prog="refresh_woolies_specials")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        _load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    written = refresh_woolies(db_url=db_url, log=log)
    return 0 if written > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
