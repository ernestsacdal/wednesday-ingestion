"""CLI orchestrator for the weekly ingestion pipeline.

Both retailers now come from authoritative sources, replacing the old
StockUp/OzBargain scrape (which proved unreliable — a price tracker, not
"this week's specials"):

  * Coles      — derived from the public hotprices.org daily dump
                 (src/refresh_coles_hotprices.py). We consume their hosted JSON,
                 so we never touch Coles' Cloudflare/Imperva gate, and cache it
                 into our own tables (source='hotprices').
  * Woolworths — the retailer's own browse API
                 (src/refresh_woolies_specials.py, source='woolies_catalogue').

Each refresh is guarded so a transient failure in one never fails the cron or
touches the other retailer's data (worst case: that retailer's list is stale for
a week, repaired on the next run).

The deep Coles half-price HISTORY (for the cycle predictor) is loaded once by
src/backfill_coles_history.py; the weekly refresh keeps the current week fresh.

Examples:
    python -m src.pipeline --write-db                 # both refreshes + images
    python -m src.pipeline --write-db --verbose       # DEBUG logging
    python -m src.pipeline --write-json out/          # dump current scrapes to JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.scrapers.base import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wednesday-ingestion",
        description="Weekly grocery half-price ingestion pipeline.",
    )
    parser.add_argument(
        "--write-json",
        metavar="DIR",
        help="Directory to dump the current Coles + Woolies scrapes as JSON (created if missing).",
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Refresh Coles + Woolies specials in Supabase via SUPABASE_DB_URL "
             "(Session Pooler URI). Loads .env from the project root if present.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)
    log.info("pipeline.start", extra={"verbose": args.verbose})

    if args.write_db:
        _maybe_load_dotenv()
        db_url = os.environ.get("SUPABASE_DB_URL")
        if not db_url:
            log.error("--write-db requested but SUPABASE_DB_URL is not set.")
            return 2

        # Coles: authoritative half-price from the hotprices.org daily dump.
        # Replaces this week's Coles specials + removes any leftover StockUp rows.
        try:
            from src.refresh_coles_hotprices import refresh_coles
            coles_written = refresh_coles(db_url=db_url, log=log)
            log.info("pipeline.coles_refreshed written=%d", coles_written)
        except Exception as e:  # noqa: BLE001 - must NOT fail the cron
            log.exception("pipeline.coles_refresh_failed err=%s", e)

        # Woolworths: authoritative half-price from the retailer's own API.
        try:
            from src.refresh_woolies_specials import refresh_woolies
            woolies_written = refresh_woolies(db_url=db_url, log=log)
            log.info("pipeline.woolies_refreshed written=%d", woolies_written)
        except Exception as e:  # noqa: BLE001 - must NOT fail the cron
            log.exception("pipeline.woolies_refresh_failed err=%s", e)

        # Backfill product images for any rows still missing one. hotprices already
        # supplies Coles images via the deterministic CDN and the Woolies API
        # supplies its own, so this only mops up the occasional new Woolies miss
        # via the cross-retailer match. Bounded to keep the cron snappy.
        try:
            from src.backfill_images import fill_missing_images
            img_stats = fill_missing_images(db_url=db_url, log=log, limit=100, workers=6)
            log.info(
                "pipeline.images_filled considered=%d hits=%d misses=%d hit_rate=%.0f%%",
                img_stats.considered, img_stats.hits, img_stats.misses,
                img_stats.hit_rate() * 100,
            )
        except Exception as e:  # noqa: BLE001 - image fill failure must NOT fail the cron
            log.exception("pipeline.images_failed err=%s", e)

        log.info("pipeline.done")
        return 0

    if args.write_json:
        from src.scrapers.hotprices import scrape as coles_scrape
        from src.scrapers.woolies_specials import build_woolies_session
        from src.scrapers.woolies_specials import scrape as woolies_scrape

        coles_out = coles_scrape(log)
        woolies_out = woolies_scrape(build_woolies_session(), log)
        out_dir = Path(args.write_json)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for name, out in (("coles_hotprices", coles_out), ("woolies_catalogue", woolies_out)):
            out_path = out_dir / f"{name}_{ts}.json"
            out_path.write_text(
                json.dumps(out.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("pipeline.json_written", extra={"path": str(out_path)})
        return 0

    log.info("pipeline: nothing to do (pass --write-db or --write-json)")
    return 0


def _maybe_load_dotenv() -> None:
    """Lightweight .env loader. No python-dotenv dependency."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        break


if __name__ == "__main__":
    sys.exit(main())
