"""Ingest the FULL product catalogue (both retailers) from the hotprices dumps.

Purpose: make EVERY product searchable + watchable — even ones that never go
half-price — so a user can find and ♥ any product to be alerted if it ever drops
to half-price.

SEARCH-ONLY by construction: this writes ONLY to `products` (no `specials` rows).
Home, the store-browse lists, the half-price counts, and the predictor all read
`specials`, which still only holds items actually on special — so they are
completely untouched. The app stays a half-price app; the full catalogue just
makes search complete (and search ranks half-price first — see migration 0018).

Runs WEEKLY (the catalogue changes slowly); the daily cron handles the
time-sensitive half-price refresh and stays light. Idempotent — upsert by
(retailer, real retailer id). Requires SUPABASE_DB_URL.

    python -m src.ingest_catalogue --retailer all --verbose
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from src.backfill_history import _upsert_products  # bulk product upsert (retailer-aware)
from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.scrapers.hotprices import HOTPRICES_URLS, fetch_dump, parse_products


def ingest_retailer(*, retailer: str, db_url: str, log: logging.Logger) -> int:
    raw = fetch_dump(retailer, log=log)
    # parse_products keeps every product with usable price history (NOT just the
    # ever-half ones) — that's the full searchable catalogue.
    products = parse_products(raw, log=log, retailer=retailer)
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            _upsert_products(cur, products, retailer, log)
        conn.commit()
    log.info("ingest_catalogue.done retailer=%s products=%d", retailer, len(products))
    return len(products)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest_catalogue")
    parser.add_argument("--retailer", choices=["all", *sorted(HOTPRICES_URLS)], default="all",
                        help="Which retailer's full catalogue to ingest (default all).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    retailers = sorted(HOTPRICES_URLS) if args.retailer == "all" else [args.retailer]
    total = 0
    for r in retailers:
        total += ingest_retailer(retailer=r, db_url=db_url, log=log)
    log.info("ingest_catalogue.all_done total_products=%d", total)
    if total == 0:
        log.error("ingest_catalogue.no_products — nothing ingested; failing the run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
