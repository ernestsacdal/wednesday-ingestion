"""Deliberate, one-off cutover: delete marketplace / non-grocery product rows.

Background (2026-06-12): ~73% of the Woolies hotprices dump is "Everyday
Market" third-party marketplace stock (car mats, perfume, $999 pet strollers
with fake was-prices) — not supermarket groceries. It polluted the app's
savings-first drops list (top 100 was 99% marketplace junk), search, category
browsing, predictions and cross-store comparisons. ``hotprices.parse_products``
now skips these items at ingest (see ``_is_marketplace``); this removes the
rows already in the DB.

Conditions (mirroring the ingest detector):
  1. woolworths products whose real stockcode (the part of ``retailer_sku``
     after ``woolworths:``) is >= 8 digits — real supermarket stockcodes are
     <= 7 digits, marketplace ids are 10 (validated: perfectly bimodal).
  2. products (either retailer) whose category label belongs ONLY to a
     denied non-grocery group (Everyday Market, Hampers & Gifting, the
     Home & Lifestyle group). Labels shared with kept codes (e.g.
     'Electronics', which is also the supermarket battery aisle 12123) are
     NOT purged by label.

DESTRUCTIVE: deletes ``products`` rows; the FK cascade (``specials`` /
``price_observations`` / ``predictions`` / ``product_aliases`` /
``prediction_accuracy``) cleans up children. Requires --confirm; the default
dry run only reports counts. The big woolworths delete runs in batches so a
single giant transaction never hits pooler limits.

    python -m src.purge_marketplace                 # dry run (counts only)
    python -m src.purge_marketplace --confirm       # actually delete

Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.scrapers.hotprices import _NON_GROCERY_CODES

_BATCH = 5000

_SKU_COND = "retailer = 'woolworths' and length(split_part(retailer_sku, ':', 2)) >= 8"
_SKU_COND_P = "p.retailer = 'woolworths' and length(split_part(p.retailer_sku, ':', 2)) >= 8"


def _denied_labels() -> list[str]:
    """Labels purgeable by category: denied-code labels not shared with kept codes."""
    mapping: dict[str, str] = json.loads(
        (Path(__file__).resolve().parent / "data" / "hotprices_categories.json")
        .read_text(encoding="utf-8")
    )
    denied = {label for code, label in mapping.items() if code in _NON_GROCERY_CODES}
    kept = {label for code, label in mapping.items() if code not in _NON_GROCERY_CODES}
    return sorted(denied - kept)


def purge(*, db_url: str, log: logging.Logger, confirm: bool) -> dict[str, int]:
    labels = _denied_labels()
    log.info("purge.category_labels %s", ", ".join(labels))
    cond = f"({_SKU_COND}) or category = any(%(labels)s)"

    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for name, sql in (
                ("products", f"select count(*) from products where {cond}"),
                ("specials", f"""select count(*) from specials s join products p on p.id = s.product_id
                                 where ({_SKU_COND_P}) or p.category = any(%(labels)s)"""),
            ):
                cur.execute(sql, {"labels": labels})
                if name == "products":
                    prod_n = cur.fetchone()[0]
                else:
                    spec_n = cur.fetchone()[0]

            if not confirm:
                log.warning(
                    "purge.dry_run would_delete products=%d (cascading specials=%d; "
                    "observations/predictions/aliases/accuracy cascade too). "
                    "Re-run with --confirm to delete.",
                    prod_n, spec_n,
                )
                return {"products": prod_n, "specials": spec_n, "deleted": 0}

        # Batched delete: each batch commits on its own so the pooler never
        # holds one enormous transaction (~52k products + ~100k+ child rows).
        deleted = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    f"""delete from products where id in (
                            select id from products where {cond} limit %(n)s)""",
                    {"labels": labels, "n": _BATCH},
                )
                batch = cur.rowcount
            conn.commit()
            deleted += batch
            if batch:
                log.info("purge.batch deleted=%d total=%d", batch, deleted)
            if batch < _BATCH:
                break

    log.info("purge.done deleted_products=%d (children cascaded)", deleted)
    return {"products": prod_n, "specials": spec_n, "deleted": deleted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="purge_marketplace")
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
