"""Backfill products.image_url via a retailer's public product sitemap.

Card-free, key-free, quota-free. Covers Coles and Woolworths (see
src/scrapers/sitemap_images.py for the mechanism). Complements
src/backfill_images.py (the Woolies internal-API path) by resolving the
house brands / name-search misses it can't.

Per-retailer row picker + write-back:

  coles       picker keys on coles_brave_fetched_at (a Coles-image attempt
              marker from migration 0012 — name predates the sitemap pivot),
              NOT image_fetched_at: the target rows all carry a stamped
              image_fetched_at from the Woolies cross-match that missed them.
              Stamps coles_brave_fetched_at on every attempt so the weekly
              cron advances past permanent misses.

  woolworths  manual one-shot to clear the Woolies-API-miss backlog: picks
              every image_url IS NULL Woolies row (they were all API-attempted
              already). No dedicated marker, so a re-run simply re-attempts the
              still-missing rows — fine for an occasional manual sweep. NOT
              wired into the weekly cron (the API step there already handles
              new Woolies products).

The catalogue sitemap is downloaded ONCE (~3-4 requests) up front; matching is
then local and the per-image CDN HEADs hit a separate, ungated host. On a
persistent Cloudflare challenge the run aborts cleanly (exit 3), leaving rows
retryable.

Run locally with:
    python -m src.backfill_sitemap_images --retailer coles --limit 1000 --verbose
    python -m src.backfill_sitemap_images --retailer woolworths --limit 1000 --verbose

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
from src.scrapers.sitemap_images import (
    RETAILERS,
    SitemapChallenged,
    build_product_index,
    build_sitemap_session,
    match_image,
)

# Small courtesy delay between CDN HEADs (a separate, ungated host).
_CDN_PACE_SECONDS = 0.2


@dataclass
class BackfillStats:
    considered: int = 0
    hits: int = 0
    misses: int = 0

    def hit_rate(self) -> float:
        if self.considered == 0:
            return 0.0
        return self.hits / self.considered


# Per-retailer picker + write-back SQL. See module docstring for the why.
_SQL = {
    "coles": {
        "select": (
            "select id::text, name from products "
            "where retailer='coles' and image_url is null "
            "and coles_brave_fetched_at is null "
            "order by last_seen desc nulls last limit %(limit)s"
        ),
        "hit": (
            "update products set image_url=%(url)s, source_product_url=%(purl)s, "
            "image_fetched_at=now(), coles_brave_fetched_at=now() "
            "where id=%(id)s::uuid"
        ),
        "miss": "update products set coles_brave_fetched_at=now() where id=%(id)s::uuid",
    },
    "woolworths": {
        "select": (
            "select id::text, name from products "
            "where retailer='woolworths' and image_url is null "
            "order by last_seen desc nulls last limit %(limit)s"
        ),
        "hit": (
            "update products set image_url=%(url)s, source_product_url=%(purl)s, "
            "image_fetched_at=now() where id=%(id)s::uuid"
        ),
        "miss": None,  # nothing to stamp; leave retryable for a later sweep
    },
}


def fill_sitemap_images(
    *, db_url: str, log: logging.Logger, retailer: str, limit: int,
) -> BackfillStats:
    """Match rows for `retailer` against its product sitemap; persist per row.

    Raises SitemapChallenged if the catalogue can't be fetched.
    """
    sql = _SQL[retailer]
    started = time.monotonic()
    stats = BackfillStats()

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql["select"], {"limit": limit})
            queue = list(cur.fetchall())

    if not queue:
        log.info("sitemap_backfill.no_work retailer=%s limit=%d", retailer, limit)
        return stats

    session = build_sitemap_session()
    index = build_product_index(session, retailer, log)  # may raise SitemapChallenged
    log.info("sitemap_backfill.start retailer=%s n=%d catalogue=%d", retailer, len(queue), len(index))

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        for i, (product_id, name) in enumerate(queue, start=1):
            stats.considered += 1
            result = match_image(index, session, retailer, name, log)
            with conn.cursor() as cur:
                if result.image_url:
                    cur.execute(
                        sql["hit"],
                        {"url": result.image_url, "purl": result.canonical_product_url, "id": product_id},
                    )
                    stats.hits += 1
                else:
                    if sql["miss"] is not None:
                        cur.execute(sql["miss"], {"id": product_id})
                    stats.misses += 1
            conn.commit()

            if i % 50 == 0:
                log.info(
                    "sitemap_backfill.progress retailer=%s done=%d/%d hits=%d misses=%d",
                    retailer, i, len(queue), stats.hits, stats.misses,
                )
            time.sleep(_CDN_PACE_SECONDS)

    log.info(
        "sitemap_backfill.done retailer=%s considered=%d hits=%d misses=%d hit_rate=%.0f%% elapsed_s=%.1f",
        retailer, stats.considered, stats.hits, stats.misses, stats.hit_rate() * 100,
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
        prog="backfill_sitemap_images",
        description="Fill products.image_url via a retailer's product sitemap.",
    )
    parser.add_argument("--retailer", required=True, choices=RETAILERS)
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
        stats = fill_sitemap_images(
            db_url=db_url, log=log, retailer=args.retailer, limit=args.limit,
        )
    except SitemapChallenged as e:
        log.error("sitemap_backfill.challenged %s", e)
        log.error("Cloudflare is rate-challenging the sitemap; wait a few minutes "
                  "(no requests) and re-run. All rows left retryable.")
        return 3

    if stats.considered > 0 and stats.hits == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
