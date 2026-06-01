"""CLI orchestrator for the weekly ingestion pipeline.

Runs both StockUp scrapers (post + Google Sheet), then optionally
writes the results to JSON, to Supabase, or both. Supabase write
maps records onto products / price_observations / specials /
scrape_runs — see src/db/writer.py.

Cross-source order matters: when --write-db is set, we write the
**sheet first, post second**, so post rows overwrite sheet rows on
(retailer, retailer_sku) collisions. That preserves the post's
richer per-row cycle metadata (the post has populated
'last_halfprice_weeks_ago'; the sheet is sparser on cycle data).

Examples:
    python -m src.pipeline                            # both scrapes, log to console
    python -m src.pipeline --write-json out/          # both scrapes + JSON dumps
    python -m src.pipeline --write-db                 # both scrapes + DB writes
    python -m src.pipeline --write-db --verbose       # DEBUG logging
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.models import ScrapeOutput
from src.scrapers import ozbargain_stockup, stockup_sheet
from src.scrapers.base import build_session, configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wednesday-ingestion",
        description="Weekly grocery half-price ingestion pipeline.",
    )
    parser.add_argument(
        "--write-json",
        metavar="DIR",
        help="Directory to write JSON dumps of each scrape (created if missing).",
    )
    parser.add_argument(
        "--write-db",
        action="store_true",
        help="Write to Supabase via SUPABASE_DB_URL (Session Pooler URI). "
             "Loads .env from the project root if present.",
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

    # Run both scrapers. Each is independent — failures in one don't
    # block the other.
    post_output = ozbargain_stockup.scrape(session, log)
    sheet_output = stockup_sheet.scrape(session, log)

    log.info(
        "pipeline.result post_status=%s post_items=%d sheet_status=%s sheet_items=%d",
        post_output.run.status,
        post_output.run.items_found,
        sheet_output.run.status,
        sheet_output.run.items_found,
    )

    if args.write_db:
        _maybe_load_dotenv()
        db_url = os.environ.get("SUPABASE_DB_URL")
        if not db_url:
            log.error("--write-db requested but SUPABASE_DB_URL is not set.")
            return 2
        from src.db.writer import write_to_db

        # Sheet first, then post — post rows overwrite sheet rows on
        # (retailer, retailer_sku) collision, preserving cycle metadata.
        if sheet_output.run.status in ("success", "partial") and sheet_output.specials:
            sheet_result = write_to_db(sheet_output, db_url=db_url, log=log)
            log.info(
                "pipeline.db_written source=sheet run_id=%s products=%d observations=%d specials=%d",
                sheet_result.scrape_run_id,
                sheet_result.products_upserted,
                sheet_result.observations_written,
                sheet_result.specials_written,
            )
        else:
            log.warning("Skipping sheet DB write (status=%s)", sheet_output.run.status)

        if post_output.run.status in ("success", "partial") and post_output.specials:
            post_result = write_to_db(post_output, db_url=db_url, log=log)
            log.info(
                "pipeline.db_written source=post run_id=%s products=%d observations=%d specials=%d",
                post_result.scrape_run_id,
                post_result.products_upserted,
                post_result.observations_written,
                post_result.specials_written,
            )
        else:
            log.warning("Skipping post DB write (status=%s)", post_output.run.status)

        # Backfill product images for any newly-added products. Bounded so the
        # weekly cron stays under its 45-min budget — at ~500ms per lookup
        # (Woolies API + cross-retailer for Coles), 100 products is ~50s
        # worst case. Expected per week: ~20 new products as the weekly
        # catalogues drift. The initial ~2,820 are filled by a manual one-
        # shot run of src.backfill_images.
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

        # Second image pass: Coles house brands the Woolies cross-match can't
        # resolve, via the Coles product sitemap + deterministic CDN. Card-free
        # and key-free. Bounded to 50 rows/run so the cron stays snappy; the
        # initial backlog is cleared by a local one-shot of
        # src.backfill_coles_images_sitemap, after which this picks up the
        # ~5-15 new Coles products each week. A persistent Cloudflare challenge
        # on the sitemap just skips this run (next week retries).
        try:
            from src.backfill_coles_images_sitemap import fill_coles_images_sitemap
            cs_stats = fill_coles_images_sitemap(db_url=db_url, log=log, limit=50)
            log.info(
                "pipeline.coles_sitemap_filled considered=%d hits=%d misses=%d hit_rate=%.0f%%",
                cs_stats.considered, cs_stats.hits, cs_stats.misses,
                cs_stats.hit_rate() * 100,
            )
        except Exception as e:  # noqa: BLE001 - must NOT fail the cron
            log.exception("pipeline.coles_sitemap_failed err=%s", e)

    if args.write_json:
        out_dir = Path(args.write_json)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for name, out in (("stockup_post", post_output), ("stockup_sheet", sheet_output)):
            out_path = out_dir / f"{name}_{ts}.json"
            out_path.write_text(
                json.dumps(out.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("pipeline.json_written", extra={"path": str(out_path)})

    # Exit code: at least one source success = 0. Both failed = 2.
    # 'no_data' from one source while the other succeeds is still 0.
    return _exit_code(post_output, sheet_output)


def _exit_code(*outputs: ScrapeOutput) -> int:
    statuses = {o.run.status for o in outputs}
    if "success" in statuses:
        return 0
    if "partial" in statuses:
        return 0
    if "no_data" in statuses:
        return 1
    return 2


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
