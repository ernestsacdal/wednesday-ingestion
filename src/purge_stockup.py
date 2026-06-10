"""Deliberate, one-off cleanup of retired StockUp data.

StockUp/OzBargain was retired as a data source (2026-06-07): Coles now comes from
the hotprices.org dump and Woolies from its own API. The old StockUp rows
(source 'stockup_post' / 'stockup_sheet') linger as inaccurate history — they
were a price tracker, ~half their "half-price" rows were wrong — and they still
feed the cycle predictor + the product-detail sale-history list. This removes
them.

DESTRUCTIVE: deletes from price_observations + specials. Requires --confirm to do
anything; a dry run (default) just reports the counts that WOULD be deleted.

    python -m src.purge_stockup                 # dry run (counts only)
    python -m src.purge_stockup --confirm       # actually delete
    python -m src.purge_stockup --retailer coles --confirm

Only touches StockUp-sourced rows. Authoritative hotprices/woolies_catalogue rows
are never affected. Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging

_STOCKUP_SOURCES = ("stockup_post", "stockup_sheet")


def purge(*, db_url: str, log: logging.Logger, retailer: str | None, confirm: bool) -> dict[str, int]:
    retailer_clause = "and p.retailer = %(retailer)s" if retailer else ""
    params: dict = {"sources": list(_STOCKUP_SOURCES)}
    if retailer:
        params["retailer"] = retailer

    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            # Count first (dry run shows this and stops).
            cur.execute(
                f"""select count(*) from price_observations po join products p on p.id = po.product_id
                    where po.source = any(%(sources)s) {retailer_clause}""",
                params,
            )
            obs_n = cur.fetchone()[0]
            cur.execute(
                f"""select count(*) from specials s join products p on p.id = s.product_id
                    where s.source = any(%(sources)s) {retailer_clause}""",
                params,
            )
            spec_n = cur.fetchone()[0]

            if not confirm:
                log.warning("purge.dry_run retailer=%s would_delete observations=%d specials=%d "
                            "(re-run with --confirm to delete)", retailer or "all", obs_n, spec_n)
                return {"observations": obs_n, "specials": spec_n, "deleted": 0}

            cur.execute(
                f"""delete from price_observations po using products p
                    where po.product_id = p.id and po.source = any(%(sources)s) {retailer_clause}""",
                params,
            )
            del_obs = cur.rowcount
            cur.execute(
                f"""delete from specials s using products p
                    where s.product_id = p.id and s.source = any(%(sources)s) {retailer_clause}""",
                params,
            )
            del_spec = cur.rowcount
        conn.commit()

    log.info("purge.done retailer=%s deleted observations=%d specials=%d",
             retailer or "all", del_obs, del_spec)
    return {"observations": del_obs, "specials": del_spec, "deleted": del_obs + del_spec}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="purge_stockup")
    parser.add_argument("--retailer", choices=["coles", "woolworths"], default=None,
                        help="Limit to one retailer (default: both).")
    parser.add_argument("--confirm", action="store_true", help="Actually delete (omit for a dry run).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    purge(db_url=db_url, log=log, retailer=args.retailer, confirm=args.confirm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
