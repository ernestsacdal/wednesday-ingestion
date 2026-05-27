"""One-shot CLI: fill products.image_url for everything that's still null.

Walks products WHERE image_url IS NULL, looks up each via
src.scrapers.product_images.lookup_image, and UPDATEs the row with the
image URL (or just stamps image_fetched_at = now() on misses so we don't
re-attempt every run).

Threading: 4-8 workers with one requests.Session per worker. The Woolies
API tolerates this fine in practice. Per-call latency is ~500ms so 6
workers gets us roughly 12 lookups/sec, ~4 min for 2.6k products.

Usage:
    python -m src.backfill_images --limit 3000 --workers 6 --verbose

Module-level entry point also exposed (`fill_missing_images`) so the
weekly pipeline can call into this with a smaller --limit each run to
mop up freshly-added products without ballooning the cron runtime.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg

from src.scrapers.base import configure_logging
from src.scrapers.product_images import (
    ImageLookupResult,
    build_image_session,
    lookup_image,
)


@dataclass
class BackfillStats:
    considered: int = 0
    hits: int = 0
    misses: int = 0
    errors: int = 0
    by_method: dict[str, int] = None

    def __post_init__(self):
        if self.by_method is None:
            self.by_method = {}

    def hit_rate(self) -> float:
        if self.considered == 0:
            return 0.0
        return self.hits / self.considered


def fill_missing_images(
    *,
    db_url: str,
    log: logging.Logger,
    limit: int = 100,
    workers: int = 6,
) -> BackfillStats:
    """Fetch image URLs for up to `limit` products whose image_url is null.

    Threading: each worker gets its own requests.Session via
    build_image_session() (which primes Akamai cookies). DB writes happen
    on the main thread, batched: each worker returns its lookup result,
    the main loop UPDATEs immediately. That keeps the DB transaction
    small and avoids a long-running connection mid-flight.
    """
    started = time.monotonic()
    stats = BackfillStats()

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            # Pick rows that have never been attempted (image_fetched_at IS NULL).
            # On re-runs we skip rows we already tried; a future maintenance
            # script can re-attempt rows older than N days if we ever want to.
            cur.execute(
                """
                select id::text, retailer, name
                  from products
                 where image_url is null
                   and image_fetched_at is null
                 order by last_seen desc nulls last
                 limit %(limit)s
                """,
                {"limit": limit},
            )
            rows = cur.fetchall()

        if not rows:
            log.info("backfill.no_work limit=%d", limit)
            return stats

        log.info("backfill.start n=%d workers=%d", len(rows), workers)

        # Build a session per worker. Slight setup cost is fine.
        sessions = [build_image_session() for _ in range(workers)]

        def task(idx_row: tuple[int, tuple[str, str, str]]) -> tuple[str, ImageLookupResult]:
            idx, (product_id, retailer, name) = idx_row
            session = sessions[idx % workers]
            result = lookup_image(session, retailer, name, log)
            return product_id, result

        with conn.cursor() as cur:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                indexed = list(enumerate(rows))
                for product_id, result in pool.map(task, indexed):
                    stats.considered += 1
                    stats.by_method[result.method] = stats.by_method.get(result.method, 0) + 1

                    if result.image_url:
                        cur.execute(
                            """
                            update products
                               set image_url = %(url)s,
                                   image_fetched_at = now()
                             where id = %(id)s::uuid
                            """,
                            {"url": result.image_url, "id": product_id},
                        )
                        stats.hits += 1
                        if stats.considered % 50 == 0:
                            log.info(
                                "backfill.progress considered=%d hits=%d rate=%.0f%%",
                                stats.considered, stats.hits, stats.hit_rate() * 100,
                            )
                    else:
                        cur.execute(
                            """
                            update products
                               set image_fetched_at = now()
                             where id = %(id)s::uuid
                            """,
                            {"id": product_id},
                        )
                        if result.error and result.error.startswith(("woolies_request_error", "woolies_parse_error")):
                            stats.errors += 1
                        else:
                            stats.misses += 1

        conn.commit()

    elapsed = time.monotonic() - started
    log.info(
        "backfill.done considered=%d hits=%d misses=%d errors=%d "
        "hit_rate=%.0f%% by_method=%s elapsed_s=%.1f",
        stats.considered, stats.hits, stats.misses, stats.errors,
        stats.hit_rate() * 100, dict(stats.by_method), elapsed,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_images",
        description="Fill products.image_url for everything that's still null.",
    )
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)

    # Allow .env loading for local runs (same pattern as pipeline.py)
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        # Lightweight .env loader so this works without sourcing.
        from pathlib import Path
        for env_path in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
            if env_path.is_file():
                for raw in env_path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip(); v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
                break
        db_url = os.environ.get("SUPABASE_DB_URL")

    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    stats = fill_missing_images(
        db_url=db_url, log=log, limit=args.limit, workers=args.workers,
    )
    # Exit non-zero only if zero hits AND zero work-considered — that's a
    # real signal something's wrong. A low hit rate is expected (Coles
    # cross-match misses on house brands) and not an error.
    if stats.considered > 0 and stats.hits == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
