"""Replace this week's Woolworths half-price specials with authoritative data.

Primary source is the real Woolworths "Half Price" category via
src/scrapers/woolies_specials (source=woolies_catalogue). If that live API is
unavailable — which is common from datacenter IPs, so the daily GitHub Actions
cron silently lost Woolies once — we FALL BACK to deriving Woolies half-price
from the hotprices Woolies dump (the same authoritative, CDN-hosted source Coles
uses; deterministic and reachable from CI). Dump-derived rows are tagged
source=hotprices. Then we delete any leftover current-week StockUp rows.

Order matters for safety:
  1. write authoritative rows first (so a failure never leaves Woolies empty),
  2. then delete the remaining stockup-sourced Woolies specials for the week.
Name-matching products get their special overwritten in step 1; the rest are
removed in step 2.

Run locally:
    python -m src.refresh_woolies_specials --verbose
    python -m src.refresh_woolies_specials --force-fallback --verbose  # test the dump path
Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from src.db.bulk_writer import bulk_write_to_db
from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.scrapers.woolies_specials import build_woolies_session, scrape


def _scrape_woolies_dump(log: logging.Logger):
    """Woolies this-week half-price derived from the hotprices dump (CI fallback)."""
    from src.scrapers.hotprices import scrape as hotprices_scrape
    return hotprices_scrape(log, retailer="woolworths")


def refresh_woolies(*, db_url: str, log: logging.Logger,
                    force_fallback: bool = False) -> int:
    """Returns the number of authoritative Woolies specials written.

    Tries the live Woolies API first, then falls back to the hotprices Woolies
    dump if the API is blocked/empty (datacenter-IP outage). ``force_fallback``
    skips the live API entirely — used to exercise the dump path in testing.
    """
    out = None
    if not force_fallback:
        try:
            out = scrape(build_woolies_session(), log)
        except Exception:  # noqa: BLE001 — fall through to the dump fallback
            log.exception("refresh_woolies.live_api_error — will try hotprices dump fallback")
            out = None

    if out is None or not out.specials:
        # Live API blocked or empty (common from GitHub Actions datacenter IPs).
        # Derive Woolies half-price from the hotprices Woolies dump instead.
        log.warning("refresh_woolies.live_api_unavailable — falling back to hotprices Woolies dump "
                    "(force_fallback=%s)", force_fallback)
        out = _scrape_woolies_dump(log)

    if not out.specials:
        # Both sources empty: deliberately keep last run's rows rather than wipe
        # Woolies. Surfaced loudly so a multi-week outage is noticed.
        log.error("refresh_woolies.no_data — both the live API and the hotprices dump returned 0 "
                  "specials; KEEPING existing rows (no write, no delete). Investigate if persistent.")
        return 0

    week_start = out.specials[0].week_start

    # A partial scrape (live API died mid-pagination) is written as upserts
    # only: pruning against an incomplete set would delete items the scrape
    # simply didn't reach. Stale extras get cleaned by the next full run.
    is_partial = out.run.status == "partial"
    if is_partial:
        log.warning("refresh_woolies.partial_scrape — writing upserts only, "
                    "skipping the current-week prune + cross-source sweep")

    # 1. Write authoritative Woolies half-price rows. write_to_db is
    # transaction-safe (all-or-nothing); if it raises, we have NOT deleted any
    # StockUp rows yet, so the worst case is stale-but-present data + a clear log.
    try:
        result = bulk_write_to_db(out, db_url=db_url, log=log, sync_week=not is_partial)
    except Exception:
        log.exception("refresh_woolies.write_failed — StockUp Woolies rows left in place "
                      "(no delete attempted); safe to retry")
        raise

    # 2. Remove any remaining current-week Woolies specials NOT from the source we
    # just wrote. This sweeps legacy StockUp rows AND — on a live<->fallback
    # transition — the other refresh source's stale rows (sync_week only prunes
    # within one source). Safe because history never writes the current week, so
    # the current week's Woolies list should equal exactly this one pull.
    # Skipped for partial scrapes (see above).
    deleted = 0
    written_source = out.specials[0].source
    if not is_partial:
        with psycopg.connect(db_url, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    delete from specials s
                    using products p
                    where s.product_id = p.id
                      and p.retailer = 'woolworths'
                      and s.week_start = %(w)s
                      and s.source <> %(src)s
                    """,
                    {"w": week_start, "src": written_source},
                )
                deleted = cur.rowcount
            conn.commit()

    log.info(
        "refresh_woolies.done written=%d deleted_other_source=%d source=%s week_start=%s",
        result.specials_written, deleted, written_source, week_start,
    )
    return result.specials_written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_woolies_specials")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--force-fallback", action="store_true",
                        help="Skip the live API and source Woolies from the hotprices dump "
                             "(exercises the CI fallback path).")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    written = refresh_woolies(db_url=db_url, log=log, force_fallback=args.force_fallback)
    return 0 if written > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
