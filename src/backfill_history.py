"""One-time (re-runnable) backfill of half-price HISTORY from a hotprices dump.

Works for either retailer (``--retailer coles|woolworths``). The recurring
refreshes write only *this week's* specials; this imports the deep history: for
every product that has ever gone half-price, it ensures the product row exists
and writes one specials + one price_observation row per historical half-price
event (source='hotprices'). That gives the cycle predictor real multi-year
history per product, so genuine medium/high-confidence predictions emerge
immediately — and it makes those products searchable + watchable even when
they're not currently on special.

Idempotent: ON CONFLICT DO NOTHING on (product_id, week_start) for specials and
(product_id, observed_at, source) for price_observations. The CURRENT week is
skipped (the live refresh owns it) to avoid double-writing the live promo.

Run locally (one-off):
    python -m src.backfill_history --retailer woolworths --verbose
    python -m src.backfill_history --retailer coles --verbose
Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.scrapers.hotprices import (
    HOTPRICES_URLS, _most_recent_wednesday, fetch_dump, parse_products,
)

_PRODUCT_BATCH = 500
_ROW_BATCH = 1000


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _upsert_products(cur, products, retailer, log) -> None:
    """Ensure a product row exists for every ever-half product.

    Keyed on the REAL retailer id ('coles:<id>' / 'woolworths:<stockcode>'), so
    distinct same-name products stay distinct. Dedup by that key first so the
    same ON CONFLICT target is never hit twice in one multi-row statement
    (Postgres rejects that).
    """
    by_sku: dict[str, tuple] = {}
    for p in products:
        sku = f"{retailer}:{p.coles_id}"
        by_sku[sku] = (
            retailer, sku, p.name, "Uncategorised",
            p.regular_cents, p.image_url, p.source_product_url,
        )
    rows = list(by_sku.values())
    total = 0
    for batch in _chunks(rows, _PRODUCT_BATCH):
        ph = ",".join(["(%s,%s,%s,%s,%s,%s::text,%s,now(),now())"] * len(batch))
        flat = [v for r in batch for v in r]
        cur.execute(
            f"""
            insert into products
                (retailer, retailer_sku, name, category, regular_price_cents,
                 image_url, source_product_url, image_fetched_at, last_seen)
            values {ph}
            on conflict (retailer, retailer_sku) do update set
                image_url = coalesce(products.image_url, excluded.image_url),
                source_product_url = coalesce(products.source_product_url, excluded.source_product_url),
                image_fetched_at = case
                    when products.image_url is null and excluded.image_url is not null then now()
                    else products.image_fetched_at end,
                last_seen = now()
            """,
            flat,
        )
        total += len(batch)
    log.info("backfill_history.products_upserted=%d retailer=%s", total, retailer)


def _load_product_ids(cur, retailer) -> dict[str, str]:
    cur.execute("select retailer_sku, id::text from products where retailer=%s", (retailer,))
    return {sku: pid for sku, pid in cur.fetchall()}


def backfill(*, retailer: str, db_url: str, log: logging.Logger) -> dict[str, int]:
    raw = fetch_dump(retailer, log=log)
    today = datetime.now(timezone.utc).date()
    products = parse_products(raw, log=log, today=today, retailer=retailer)
    ever_half = [p for p in products if p.events]
    current_week = _most_recent_wednesday(today)
    log.info("backfill_history.start retailer=%s ever_half=%d current_week=%s",
             retailer, len(ever_half), current_week)

    specials_rows: list[tuple] = []
    obs_rows: list[tuple] = []

    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            _upsert_products(cur, ever_half, retailer, log)
        conn.commit()
        with conn.cursor() as cur:
            ids = _load_product_ids(cur, retailer)
        log.info("backfill_history.product_ids_loaded=%d", len(ids))

        skipped_no_id = 0
        for p in ever_half:
            pid = ids.get(f"{retailer}:{p.coles_id}")
            if pid is None:
                skipped_no_id += 1
                continue
            for ev in p.events:
                # The live refresh owns the current promo; skip current-week events.
                if ev.on_date >= current_week:
                    continue
                week_end = ev.on_date + timedelta(days=6)
                specials_rows.append((
                    pid, ev.on_date, week_end, ev.regular_cents, ev.sale_cents,
                    ev.discount_pct, True, "hotprices",
                ))
                obs_rows.append((
                    pid, ev.sale_cents, True, ev.discount_pct, ev.on_date, "hotprices",
                ))

        spec_written = 0
        with conn.cursor() as cur:
            for batch in _chunks(specials_rows, _ROW_BATCH):
                ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s)"] * len(batch))
                flat = [v for r in batch for v in r]
                cur.execute(
                    f"""
                    insert into specials
                        (product_id, week_start, week_end, regular_price_cents,
                         sale_price_cents, discount_pct, is_half_price, source)
                    values {ph}
                    on conflict (product_id, week_start) do nothing
                    """,
                    flat,
                )
                spec_written += cur.rowcount
        conn.commit()

        obs_written = 0
        with conn.cursor() as cur:
            for batch in _chunks(obs_rows, _ROW_BATCH):
                ph = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(batch))
                flat = [v for r in batch for v in r]
                cur.execute(
                    f"""
                    insert into price_observations
                        (product_id, price_cents, is_special, discount_pct, observed_at, source)
                    values {ph}
                    on conflict (product_id, observed_at, source) do nothing
                    """,
                    flat,
                )
                obs_written += cur.rowcount
        conn.commit()

    stats = {
        "retailer": retailer,
        "ever_half": len(ever_half),
        "event_rows": len(specials_rows),
        "specials_inserted": spec_written,
        "observations_inserted": obs_written,
        "skipped_no_id": skipped_no_id,
    }
    log.info("backfill_history.done %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backfill_history")
    parser.add_argument("--retailer", choices=sorted(HOTPRICES_URLS), default="coles",
                        help="Which retailer's hotprices dump to backfill (default coles).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    stats = backfill(retailer=args.retailer, db_url=db_url, log=log)
    return 0 if stats["specials_inserted"] >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
