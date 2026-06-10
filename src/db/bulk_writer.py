"""Fast bulk writer for a ScrapeOutput — the set-based counterpart of writer.py.

writer.write_to_db issues ~3 statements per special (one round-trip each), which
is fine for tiny scrapes but ~12 minutes for ~1,300 rows over a high-latency
pooler (≈190ms RTT) — slow enough to risk timeouts. This module writes the same
three tables (products / price_observations / specials) with multi-row VALUES
batches, turning thousands of round-trips into a few dozen.

Semantics match writer.write_to_db exactly (same ON CONFLICT upserts), so the
refresh scripts can swap one for the other. Used by refresh_coles_hotprices and
refresh_woolies_specials. The deep history backfill has its own bulk path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg

from src.db.writer import WriteResult, product_sku
from src.models import ScrapeOutput, WeeklySpecial

_PRODUCT_BATCH = 500
_ROW_BATCH = 1000


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _dedup_by_sku(specials: list[WeeklySpecial]) -> list[WeeklySpecial]:
    """Last-wins dedup on (retailer, product_sku) so a multi-row upsert never
    tries to affect the same conflict target twice (Postgres rejects that)."""
    by_key: dict[tuple[str, str], WeeklySpecial] = {}
    for s in specials:
        by_key[(s.retailer, product_sku(s))] = s
    return list(by_key.values())


def _insert_scrape_run(cur: psycopg.Cursor, output: ScrapeOutput, items: int) -> str:
    cur.execute(
        """
        insert into scrape_runs (source, run_at, status, items_found, duration_ms, error, notes)
        values (%(source)s, %(run_at)s, %(status)s, %(items)s, %(duration_ms)s, %(error)s, %(notes)s)
        returning id::text
        """,
        {
            "source": output.run.source,
            "run_at": output.run.started_at,
            "status": output.run.status,
            "items": items,
            "duration_ms": output.run.duration_ms,
            "error": output.run.error,
            "notes": output.run.notes or output.run.source_url,
        },
    )
    return cur.fetchone()[0]


def _upsert_products(cur: psycopg.Cursor, specials: list[WeeklySpecial]) -> dict[tuple[str, str], str]:
    """Bulk upsert products; return {(retailer, retailer_sku) -> id}."""
    now = datetime.now(timezone.utc)
    ids: dict[tuple[str, str], str] = {}
    for batch in _chunks(specials, _PRODUCT_BATCH):
        rows = []
        for s in batch:
            sku = product_sku(s)
            img_at = now if s.image_url else None
            rows.append((s.retailer, sku, s.product_name, s.category,
                         s.regular_price_cents, s.image_url, img_at, now))
        ph = ",".join(["(%s,%s,%s,%s,%s,%s::text,%s,%s)"] * len(batch))
        flat = [v for r in rows for v in r]
        cur.execute(
            f"""
            insert into products
                (retailer, retailer_sku, name, category, regular_price_cents,
                 image_url, image_fetched_at, last_seen)
            values {ph}
            on conflict (retailer, retailer_sku) do update set
                name = excluded.name,
                category = coalesce(products.category, excluded.category),
                regular_price_cents = greatest(products.regular_price_cents, excluded.regular_price_cents),
                image_url = coalesce(products.image_url, excluded.image_url),
                image_fetched_at = case
                    when products.image_url is null and excluded.image_url is not null then now()
                    else products.image_fetched_at end,
                last_seen = now()
            returning id::text, retailer, retailer_sku
            """,
            flat,
        )
        for pid, retailer, sku in cur.fetchall():
            ids[(retailer, sku)] = pid
    return ids


def _insert_observations(cur, specials, ids) -> int:
    written = 0
    rows = []
    for s in specials:
        pid = ids.get((s.retailer, product_sku(s)))
        if pid is None:
            continue
        rows.append((pid, s.sale_price_cents, True, s.discount_pct, s.week_start, s.source))
    for batch in _chunks(rows, _ROW_BATCH):
        ph = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(batch))
        flat = [v for r in batch for v in r]
        cur.execute(
            f"""
            insert into price_observations
                (product_id, price_cents, is_special, discount_pct, observed_at, source)
            values {ph}
            on conflict (product_id, observed_at, source) do update set
                price_cents = excluded.price_cents,
                is_special = excluded.is_special,
                discount_pct = excluded.discount_pct
            """,
            flat,
        )
        written += len(batch)
    return written


def _upsert_specials(cur, specials, ids) -> int:
    written = 0
    rows = []
    for s in specials:
        pid = ids.get((s.retailer, product_sku(s)))
        if pid is None:
            continue
        rows.append((pid, s.week_start, s.week_end, s.regular_price_cents,
                     s.sale_price_cents, s.discount_pct, s.is_half_price, s.source))
    for batch in _chunks(rows, _ROW_BATCH):
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s)"] * len(batch))
        flat = [v for r in batch for v in r]
        cur.execute(
            f"""
            insert into specials
                (product_id, week_start, week_end, regular_price_cents,
                 sale_price_cents, discount_pct, is_half_price, source)
            values {ph}
            on conflict (product_id, week_start) do update set
                sale_price_cents = excluded.sale_price_cents,
                regular_price_cents = excluded.regular_price_cents,
                discount_pct = excluded.discount_pct,
                is_half_price = excluded.is_half_price,
                week_end = excluded.week_end,
                source = excluded.source
            """,
            flat,
        )
        written += len(batch)
    return written


def _sync_current_week(cur, specials, ids, log) -> int:
    """Delete this-week specials for the same (week_start, source) that aren't in
    the set just written — so the current week's list EXACTLY equals the latest
    authoritative pull. Needed for daily cadence: an item half-price one day but
    off the list the next would otherwise leave a stale 'half-price' row for the
    rest of the week. Runs in the same transaction as the write (no empty window).
    Safe because the current week's rows for a refresh source come only from that
    refresh (the history backfill writes past weeks only)."""
    week_start = specials[0].week_start
    source = specials[0].source
    kept = list({pid for pid in ids.values()})
    cur.execute(
        """
        delete from specials
        where week_start = %(w)s and source = %(src)s
          and not (product_id = any(%(kept)s::uuid[]))
        """,
        {"w": week_start, "src": source, "kept": kept},
    )
    pruned = cur.rowcount
    if pruned:
        log.info("bulk.sync_week pruned_stale=%d week=%s source=%s", pruned, week_start, source)
    return pruned


def bulk_write_to_db(output: ScrapeOutput, *, db_url: str, log: logging.Logger,
                     sync_week: bool = False) -> WriteResult:
    """Set-based equivalent of writer.write_to_db. Same tables, far fewer trips.

    sync_week=True makes the current week's specials for this source exactly match
    the written set (prunes stale rows) — use it for the recurring daily refreshes.
    """
    specials = _dedup_by_sku(output.specials)
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            scrape_run_id = _insert_scrape_run(cur, output, len(specials))
            ids = _upsert_products(cur, specials)
            obs = _insert_observations(cur, specials, ids)
            spec = _upsert_specials(cur, specials, ids)
            if sync_week and specials:
                _sync_current_week(cur, specials, ids, log)
        conn.commit()
    log.info(
        "bulk.write.done run_id=%s products=%d observations=%d specials=%d",
        scrape_run_id, len(ids), obs, spec,
    )
    return WriteResult(
        scrape_run_id=scrape_run_id,
        products_upserted=len(ids),
        observations_written=obs,
        specials_written=spec,
    )
