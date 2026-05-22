"""CLI orchestrator for the weekly ingestion pipeline.

For now: runs the StockUp OzBargain scraper, writes results to JSON, prints
a summary. Once a Supabase project exists, --write-db will map records to
the products / price_observations / specials / scrape_runs tables.

Examples:
    python -m src.pipeline                            # scrape, log to console, no writes
    python -m src.pipeline --write-json out/          # scrape and dump to out/<timestamp>.json
    python -m src.pipeline --verbose                  # debug logging
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.scrapers import ozbargain_stockup
from src.scrapers.base import build_session, configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wednesday-ingestion",
        description="Weekly grocery half-price ingestion pipeline.",
    )
    parser.add_argument(
        "--write-json",
        metavar="DIR",
        help="Directory to write a JSON dump of the scrape (created if missing).",
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Write to Supabase (requires SUPABASE_URL + SUPABASE_SERVICE_KEY env vars). "
             "Not yet implemented — waiting on Supabase project creation.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)
    log.info("pipeline.start", extra={"verbose": args.verbose})

    session = build_session()
    output = ozbargain_stockup.scrape(session, log)

    # Summary line — easy to eyeball in CI logs.
    summary = {
        "status": output.run.status,
        "source": output.run.source,
        "items": output.run.items_found,
        "duration_ms": output.run.duration_ms,
        "week_start": output.specials[0].week_start.isoformat() if output.specials else None,
        "url": output.run.source_url,
    }
    log.info("pipeline.result %s", json.dumps(summary))

    if args.write_db:
        log.warning("--write-db requested but Supabase target not configured yet; skipping.")

    if args.write_json:
        out_dir = Path(args.write_json)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"stockup_post_{ts}.json"
        out_path.write_text(
            json.dumps(output.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("pipeline.json_written", extra={"path": str(out_path)})

    # Exit code: 0 on success, 1 on no_data, 2 on failure (so cron can alert on 2+).
    return {"success": 0, "no_data": 1, "partial": 0, "failed": 2}.get(output.run.status, 2)


if __name__ == "__main__":
    sys.exit(main())
