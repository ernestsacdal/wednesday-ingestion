"""Backfill products.image_url for Coles rows via the Coles product sitemap.

Card-free, key-free, quota-free companion to src/backfill_images.py (the
Woolies-API path). Resolves the Coles house brands the Woolies cross-match
can't — the bulk of the remaining image-coverage gap — by matching each
name-only Coles product against Coles' public product sitemap and building the
deterministic CDN image URL (see src/scrapers/coles_sitemap.py).

Walks ``products WHERE retailer='coles' AND image_url IS NULL AND
coles_brave_fetched_at IS NULL`` and, for each row:
  * HIT             -> set image_url + source_product_url + image_fetched_at
                       + coles_brave_fetched_at
  * confident miss  -> stamp coles_brave_fetched_at = now() (don't re-attempt)

The picker keys on coles_brave_fetched_at (a Coles-image attempt marker added
in migration 0012 — the name predates the sitemap pivot) rather than
image_fetched_at, because the target rows all carry a stamped image_fetched_at
from the Woolies pass that already missed them.

The catalogue sitemap is downloaded ONCE (~3 requests) up front; all matching
is then local and the per-image CDN HEADs hit a separate, ungated host. If
Cloudflare persistently challenges the sitemap, the run aborts cleanly (exit 3)
and leaves every row retryable.

Run locally with:
    python -m src.backfill_coles_images_sitemap --limit 1000 --verbose

Requires SUPABASE_DB_URL (env or .env). No API key.
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
from src.scrapers.coles_sitemap import (
    SitemapChallenged,
    build_coles_product_index,
    build_sitemap_session,
    match_coles_image,
)

# Small courtesy delay between CDN HEADs (a separate, ungated host).
_CDN_PACE_SECONDS = 0.2


@dataclass
class BackfillStats:
    considered: int = 0   # rows attempted this run
    hits: int = 0         # image_url found + verified
    misses: int = 0       # confident miss (no match / CDN 404) — stamped

    def hit_rate(self) -> float:
        if self.considered == 0:
            return 0.0
        return self.hits / self.considered


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


def fill_coles_images_sitemap(
    *, db_url: str, log: logging.Logger, limit: int,
) -> BackfillStats:
    """Match Coles rows against the product sitemap; persist per row.

    Commits after every row so a mid-run abort keeps progress. Raises
    SitemapChallenged if the catalogue can't be fetched (caller decides
    whether that's fatal).
    """
    started = time.monotonic()
    stats = BackfillStats()

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_QUEUE, {"limit": limit})
            queue = list(cur.fetchall())

    if not queue:
        log.info("coles_sitemap.no_work limit=%d", limit)
        return stats

    session = build_sitemap_session()
    # Build the catalogue index once (may raise SitemapChallenged).
    index = build_coles_product_index(session, log)
    log.info("coles_sitemap.start n=%d catalogue=%d", len(queue), len(index))

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        for i, (product_id, name) in enumerate(queue, start=1):
            stats.considered += 1
            result = match_coles_image(index, session, name, log)
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
                else:
                    cur.execute(_STAMP_MISS, {"id": product_id})
                    stats.misses += 1
            conn.commit()

            if i % 50 == 0:
                log.info(
                    "coles_sitemap.progress done=%d/%d hits=%d misses=%d",
                    i, len(queue), stats.hits, stats.misses,
                )
            time.sleep(_CDN_PACE_SECONDS)

    log.info(
        "coles_sitemap.done considered=%d hits=%d misses=%d hit_rate=%.0f%% elapsed_s=%.1f",
        stats.considered, stats.hits, stats.misses, stats.hit_rate() * 100,
        time.monotonic() - started,
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
        prog="backfill_coles_images_sitemap",
        description="Fill products.image_url for Coles rows via the Coles product sitemap.",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        _load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    try:
        stats = fill_coles_images_sitemap(db_url=db_url, log=log, limit=args.limit)
    except SitemapChallenged as e:
        log.error("coles_sitemap.challenged %s", e)
        log.error("Coles' Cloudflare is rate-challenging the sitemap; wait a few "
                  "minutes (no requests) and re-run. All rows left retryable.")
        return 3

    if stats.considered > 0 and stats.hits == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
