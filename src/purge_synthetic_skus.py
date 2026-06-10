"""Deliberate, one-off cutover: delete the old synthetic-name-keyed product rows.

Background (real-SKU rekey, 2026-06-10): products used to be keyed on a synthetic
``stockup:<normalised_name>`` SKU, which collapsed two distinct products that
happened to share a name into one row (the root cause of the ~1% price mismatch
+ dedup coverage loss). Every ingest path now keys on the REAL retailer id
(``coles:<id>`` / ``woolworths:<stockcode>``), and the data has been re-ingested
under those keys. This removes the now-orphaned synthetic rows.

DESTRUCTIVE: deletes ``products`` rows whose ``retailer_sku`` starts with
``stockup:``. The FK cascade (``specials`` / ``price_observations`` /
``predictions`` are ``on delete cascade``) cleans up their children automatically.
Requires --confirm to do anything; a dry run (default) just reports the counts.

Note: on-device watchlists + ``device_watchlists.watched_product_ids`` reference
product UUIDs that change in the rekey, so those go stale and re-sync on the next
app launch (acceptable pre-launch).

    python -m src.purge_synthetic_skus                 # dry run (counts only)
    python -m src.purge_synthetic_skus --confirm       # actually delete

Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging

_SYNTHETIC_PREFIX = "stockup:%"


def purge(*, db_url: str, log: logging.Logger, confirm: bool) -> dict[str, int]:
    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            # Count the synthetic product rows + the children that would cascade.
            cur.execute(
                "select count(*) from products where retailer_sku like %(p)s",
                {"p": _SYNTHETIC_PREFIX},
            )
            prod_n = cur.fetchone()[0]
            cur.execute(
                """select count(*) from specials s join products p on p.id = s.product_id
                   where p.retailer_sku like %(p)s""",
                {"p": _SYNTHETIC_PREFIX},
            )
            spec_n = cur.fetchone()[0]
            cur.execute(
                """select count(*) from price_observations po join products p on p.id = po.product_id
                   where p.retailer_sku like %(p)s""",
                {"p": _SYNTHETIC_PREFIX},
            )
            obs_n = cur.fetchone()[0]
            cur.execute(
                """select count(*) from predictions pr join products p on p.id = pr.product_id
                   where p.retailer_sku like %(p)s""",
                {"p": _SYNTHETIC_PREFIX},
            )
            pred_n = cur.fetchone()[0]

            if not confirm:
                log.warning(
                    "purge.dry_run would_delete products=%d (cascades: specials=%d observations=%d "
                    "predictions=%d). Re-run with --confirm to delete.",
                    prod_n, spec_n, obs_n, pred_n,
                )
                return {"products": prod_n, "specials": spec_n,
                        "observations": obs_n, "predictions": pred_n, "deleted": 0}

            # Delete the product rows; FK cascade handles the children.
            cur.execute(
                "delete from products where retailer_sku like %(p)s",
                {"p": _SYNTHETIC_PREFIX},
            )
            del_prod = cur.rowcount
        conn.commit()

    log.info("purge.done deleted_products=%d (children cascaded: specials~%d observations~%d "
             "predictions~%d)", del_prod, spec_n, obs_n, pred_n)
    return {"products": del_prod, "specials": spec_n,
            "observations": obs_n, "predictions": pred_n, "deleted": del_prod}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="purge_synthetic_skus")
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

    purge(db_url=db_url, log=log, confirm=args.confirm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
