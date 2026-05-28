"""Local-only one-shot CLI: fill products.image_url for Coles rows that
the Woolies-API path (src/backfill_images.py) couldn't resolve.

Walks `products WHERE retailer='coles' AND image_url IS NULL AND
image_fetched_at IS NULL`, launches a small pool of Playwright Chromium
contexts, and updates each row with the discovered image URL (or just
stamps image_fetched_at = now() on misses so we don't re-attempt every
run).

Run locally with:
    python -m src.backfill_coles_images --limit 1000 --workers 2 --verbose

Workers default to 2 because each Playwright context is heavy (~150MB
RAM). At ~6s per lookup * 0.6 concurrency = ~50 min for 500 products
on a typical laptop.

This script is intentionally NOT wired into the weekly GHA cron — see
src/scrapers/coles_playwright.py docstring for why.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from src.scrapers.base import configure_logging
from src.scrapers.coles_playwright import build_browser_context, lookup_coles_image


@dataclass
class BackfillStats:
    considered: int = 0
    hits: int = 0
    misses: int = 0
    errors: int = 0

    def hit_rate(self) -> float:
        if self.considered == 0:
            return 0.0
        return self.hits / self.considered


async def fill_coles_images(
    *, db_url: str, log: logging.Logger, limit: int, workers: int,
) -> BackfillStats:
    """Walk products needing Coles image lookup; fan out to N browser contexts.

    Each worker runs `lookup_coles_image` against its share of the queue
    and posts results back via an asyncio.Queue. The main coroutine
    drains results + writes them to Supabase in batches.
    """
    started = time.monotonic()
    stats = BackfillStats()

    # Snapshot the queue from Supabase up front.
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id::text, name
                  from products
                 where retailer = 'coles'
                   and image_url is null
                   and image_fetched_at is null
                 order by last_seen desc nulls last
                 limit %(limit)s
                """,
                {"limit": limit},
            )
            queue = list(cur.fetchall())

    if not queue:
        log.info("coles_backfill.no_work limit=%d", limit)
        return stats

    log.info("coles_backfill.start n=%d workers=%d", len(queue), workers)

    # Slice the queue across N workers. Each worker holds its own
    # BrowserContext (heavy) so we keep N small (2-3).
    chunks: list[list[tuple[str, str]]] = [[] for _ in range(workers)]
    for i, item in enumerate(queue):
        chunks[i % workers].append(item)

    # Each worker yields (product_id, ImageLookupResult). We collect
    # them all then write in a single transaction at the end.
    async def worker(idx: int, items: list[tuple[str, str]]) -> list[tuple[str, object]]:
        results: list[tuple[str, object]] = []
        async with build_browser_context() as ctx:
            for product_id, name in items:
                r = await lookup_coles_image(ctx, name, log)
                results.append((product_id, r))
                if (len(results) + idx * 50) % 25 == 0:
                    log.info(
                        "coles_backfill.worker%d_progress done=%d total=%d",
                        idx, len(results), len(items),
                    )
        return results

    workers_results = await asyncio.gather(
        *(worker(i, chunk) for i, chunk in enumerate(chunks) if chunk)
    )

    # Flatten + write back.
    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            for batch in workers_results:
                for product_id, result in batch:
                    stats.considered += 1
                    if getattr(result, "image_url", None):
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
                    else:
                        cur.execute(
                            """
                            update products
                               set image_fetched_at = now()
                             where id = %(id)s::uuid
                            """,
                            {"id": product_id},
                        )
                        err = getattr(result, "error", "") or ""
                        if err.startswith(("coles_unexpected", "coles_page_load")):
                            stats.errors += 1
                        else:
                            stats.misses += 1
        conn.commit()

    elapsed = time.monotonic() - started
    log.info(
        "coles_backfill.done considered=%d hits=%d misses=%d errors=%d "
        "hit_rate=%.0f%% elapsed_s=%.1f",
        stats.considered, stats.hits, stats.misses, stats.errors,
        stats.hit_rate() * 100, elapsed,
    )
    return stats


def _load_dotenv() -> None:
    """Lightweight .env loader (mirrors pipeline.py)."""
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
    parser = argparse.ArgumentParser(
        prog="backfill_coles_images",
        description="Fill products.image_url for Coles rows via Playwright (local-only).",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        _load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    stats = asyncio.run(
        fill_coles_images(
            db_url=db_url, log=log, limit=args.limit, workers=args.workers,
        )
    )
    if stats.considered > 0 and stats.hits == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
