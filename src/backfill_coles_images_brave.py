"""Backfill products.image_url for Coles rows via Brave Search + the Coles CDN.

Companion to src/backfill_coles_images.py (the abandoned Playwright path,
left as reference). This one resolves Coles house brands — the bulk of the
remaining image-coverage gap — without touching Coles' Cloudflare: a
``site:coles.com.au`` Brave query recovers the real product ID, and the
image comes from the deterministic CDN (see src/scrapers/coles_brave.py).

Walks ``products WHERE retailer='coles' AND image_url IS NULL AND
coles_brave_fetched_at IS NULL`` and, for each row:
  * HIT             -> set image_url + source_product_url + image_fetched_at
                       + coles_brave_fetched_at
  * confident miss  -> stamp coles_brave_fetched_at = now() (don't re-attempt)
  * transient error -> leave the row untouched (retry on a later run)

The picker keys on coles_brave_fetched_at — NOT image_fetched_at — because
the target rows are exactly the ones the Woolies-API pass already missed (so
they all carry a stamped image_fetched_at). See migration 0012 for the full
rationale.

Synchronous + single-threaded: Brave's free tier caps at 1 query/sec, so we
pace with time.sleep(1.1) and there's nothing to parallelise. The
``--max-queries`` ceiling protects the 2,000/month free budget against a
runaway run.

Run locally with:
    python -m src.backfill_coles_images_brave --limit 1000 --max-queries 2000 --verbose

Requires BRAVE_SEARCH_API_KEY + SUPABASE_DB_URL (env or .env).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from src.scrapers.base import configure_logging
from src.scrapers.coles_brave import build_brave_session, lookup_coles_image_brave

# Brave free tier: 1 query/sec. 1.1s gives a little headroom.
_BRAVE_PACE_SECONDS = 1.1


@dataclass
class BackfillStats:
    considered: int = 0   # rows attempted this run
    hits: int = 0         # image_url found + verified
    misses: int = 0       # confident miss (no match / CDN 404) — stamped
    transient: int = 0    # brave_request_error — left retryable

    def hit_rate(self) -> float:
        """Hits over *resolved* attempts (excludes transient failures)."""
        resolved = self.hits + self.misses
        if resolved == 0:
            return 0.0
        return self.hits / resolved


_SELECT_QUEUE = """
    select id::text, name
      from products
     where retailer = 'coles'
       and image_url is null
       and coles_brave_fetched_at is null
     order by last_seen desc nulls last
     limit %(limit)s
"""

_UPDATE_HIT = """
    update products
       set image_url = %(url)s,
           source_product_url = %(purl)s,
           image_fetched_at = now(),
           coles_brave_fetched_at = now()
     where id = %(id)s::uuid
"""

_STAMP_MISS = """
    update products
       set coles_brave_fetched_at = now()
     where id = %(id)s::uuid
"""


def fill_coles_images_brave(
    *,
    db_url: str,
    log: logging.Logger,
    limit: int,
    max_queries: int,
    api_key: str,
) -> BackfillStats:
    """Resolve Coles images via Brave + CDN; persist per row.

    Commits after every row so a mid-run abort (Ctrl+C, quota exhaustion)
    keeps the progress made so far. Transient rows stay NULL so a later run
    retries them.
    """
    started = time.monotonic()
    stats = BackfillStats()

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_QUEUE, {"limit": limit})
            queue = list(cur.fetchall())

    if not queue:
        log.info("coles_brave.no_work limit=%d", limit)
        return stats

    # Cap Brave calls so a runaway can't blow the monthly free budget.
    if max_queries is not None and len(queue) > max_queries:
        log.info(
            "coles_brave.capped queue=%d max_queries=%d", len(queue), max_queries
        )
        queue = queue[:max_queries]

    log.info("coles_brave.start n=%d", len(queue))
    session = build_brave_session()

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        for i, (product_id, name) in enumerate(queue, start=1):
            stats.considered += 1
            result = lookup_coles_image_brave(session, name, log, api_key=api_key)
            err = result.error or ""

            with conn.cursor() as cur:
                if result.image_url:
                    cur.execute(
                        _UPDATE_HIT,
                        {
                            "url": result.image_url,
                            "purl": result.canonical_product_url,
                            "id": product_id,
                        },
                    )
                    stats.hits += 1
                elif err.startswith("brave_request_error"):
                    # Transient — leave the row NULL so a later run retries.
                    stats.transient += 1
                else:
                    cur.execute(_STAMP_MISS, {"id": product_id})
                    stats.misses += 1
            conn.commit()

            if i % 25 == 0:
                log.info(
                    "coles_brave.progress done=%d/%d hits=%d misses=%d transient=%d",
                    i, len(queue), stats.hits, stats.misses, stats.transient,
                )

            # Respect Brave's 1 req/sec — only between calls, not after the last.
            if i < len(queue):
                time.sleep(_BRAVE_PACE_SECONDS)

    elapsed = time.monotonic() - started
    log.info(
        "coles_brave.done considered=%d hits=%d misses=%d transient=%d "
        "hit_rate=%.0f%% elapsed_s=%.1f",
        stats.considered, stats.hits, stats.misses, stats.transient,
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
        prog="backfill_coles_images_brave",
        description="Fill products.image_url for Coles rows via Brave Search + CDN.",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-queries", type=int, default=2000)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL") or not os.environ.get("BRAVE_SEARCH_API_KEY"):
        _load_dotenv()

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not api_key:
        log.error("BRAVE_SEARCH_API_KEY not set — sign up at brave.com/search/api")
        return 2

    stats = fill_coles_images_brave(
        db_url=db_url,
        log=log,
        limit=args.limit,
        max_queries=args.max_queries,
        api_key=api_key,
    )
    # Signal failure if we attempted resolved work but matched nothing
    # (e.g. an invalid key 401s every call → all transient, hits == 0).
    if stats.considered > 0 and stats.hits == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
