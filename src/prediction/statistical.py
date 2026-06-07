"""v1 statistical cycle predictor.

Given per-product half-price history, emits a predicted next-sale window with
confidence. Pure Python — no numpy/pandas/Prophet. The math is intentionally
boring: mean of intervals ± stddev gives the window; cycle_count + dispersion
ratio (stddev / mean) drives confidence.

Phase 1a input: a single ScrapeOutput JSON. Each WeeklySpecial that's
currently half-price + has a `last_halfprice_weeks_ago` value gives us ONE
historical interval. That's enough to seed the predictor for products with
1+ visible cycle, gated by confidence.

Phase 1b input (when Supabase exists): rolling history from price_observations
across many scrape runs — produces real ≥8-cycle predictions per the plan's
gating rule.

CLI:
    python -m src.prediction.statistical INPUT_JSON [--output DIR] [--min-cycles N] [--verbose]

Example:
    python -m src.prediction.statistical data/runs/stockup_post_*.json \
        --output data/predictions
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.models import (
    ConfidenceTier, Prediction, PredictionRunSummary, Retailer, WeeklySpecial,
)
from src.scrapers.base import configure_logging

# Minimum number of historical intervals required to emit a prediction.
# Plan calls for ≥8 once we have backfill; before that we relax to 1 so the
# predictor produces visible (but low-confidence) output on a single scrape.
DEFAULT_MIN_CYCLES = 1

# Hard upper bound on prediction window half-width, so a single noisy interval
# doesn't produce a 6-month "could be anywhere" range that's useless.
MAX_WINDOW_HALFWIDTH_WEEKS = 4

# Floor on stddev when n=1 (no dispersion observable yet). Gives the window
# some honest fuzz instead of pretending we know the date exactly.
STDDEV_FLOOR_WEEKS_N1 = 1.5

# Stddev floor for low-cycle predictions (n in 2..3). With only 2-3 intervals,
# a coincidentally-zero stddev would produce a zero-width window — overstates
# confidence. Floor to 1.0 weeks until we have ≥4 cycles to be honest about it.
STDDEV_FLOOR_WEEKS_LOW_N = 1.0

# Intervals below this many weeks likely indicate the same multi-week sale
# rather than a real cycle (groceries rarely cycle weekly). Filtered out.
MIN_CYCLE_INTERVAL_WEEKS = 2


def _load_specials_from_json(path: Path, log: logging.Logger) -> list[WeeklySpecial]:
    """Hydrate WeeklySpecial dataclasses from a scraper-produced JSON dump."""
    data = json.loads(path.read_text(encoding="utf-8"))
    specials_raw = data.get("specials") or []
    specials: list[WeeklySpecial] = []
    for r in specials_raw:
        specials.append(WeeklySpecial(
            retailer=r["retailer"],
            product_name=r["product_name"],
            category=r["category"],
            regular_price_cents=r["regular_price_cents"],
            sale_price_cents=r["sale_price_cents"],
            discount_pct=r["discount_pct"],
            is_half_price=r["is_half_price"],
            last_halfprice_raw=r["last_halfprice_raw"],
            last_halfprice_weeks_ago=r["last_halfprice_weeks_ago"],
            last_halfprice_retailer=r["last_halfprice_retailer"],
            week_start=date.fromisoformat(r["week_start"]),
            week_end=date.fromisoformat(r["week_end"]),
            source=r["source"],
            source_url=r["source_url"],
            scraped_at=datetime.fromisoformat(r["scraped_at"]),
        ))
    log.info("predict.loaded", extra={"path": str(path), "specials": len(specials)})
    return specials


def _key(s: WeeklySpecial) -> tuple[Retailer, str]:
    """Per-product grouping key. Names normalised lightly to dedup near-dupes."""
    return s.retailer, s.product_name.strip()


def _derive_intervals(entries: list[tuple[date, int | None]]) -> list[int]:
    """Derive cycle intervals (in weeks) for one product.

    Two sources, combined as a deduplicated set of inferred sale dates:
      1. Each entry's `week_start` is an observed half-price week.
      2. Each entry's `last_halfprice_weeks_ago` hint implies a prior sale
         at week_start - N weeks (when present, e.g. from current-week
         StockUp posts that include the cycle column).

    Sales within 1 week of each other are treated as the same event to
    avoid double-counting hint+observed overlap.
    """
    sale_dates: list[date] = []
    for week_start, hint in entries:
        sale_dates.append(week_start)
        if hint is not None and hint > 0:
            sale_dates.append(week_start - timedelta(weeks=hint))

    sale_dates.sort()
    # Dedupe near-duplicate dates (within 7 days).
    deduped: list[date] = []
    for d in sale_dates:
        if deduped and (d - deduped[-1]).days < 7:
            continue
        deduped.append(d)

    intervals: list[int] = []
    for i in range(1, len(deduped)):
        weeks = round((deduped[i] - deduped[i - 1]).days / 7)
        # Filter intervals < MIN_CYCLE_INTERVAL_WEEKS — likely the same
        # multi-week sale rather than a real cycle.
        if weeks >= MIN_CYCLE_INTERVAL_WEEKS:
            intervals.append(weeks)
    return intervals


def _confidence_tier(score: float) -> ConfidenceTier:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _build_rationale(
    *,
    cycle_count: int,
    mean_w: float,
    stddev_w: float,
    last_sale: date,
    win_start: date,
    win_end: date,
    today: date,
) -> str:
    """Plain-English explanation for the prediction card."""
    days_since = (today - last_sale).days
    if days_since <= 7:
        sale_status = "Currently half-price"
    else:
        sale_status = f"Last seen half-price {days_since // 7} weeks ago"

    if cycle_count == 1:
        return (
            f"{sale_status}. Only one historical cycle observed so far — predicted window "
            f"{win_start.isoformat()} to {win_end.isoformat()} is a rough estimate."
        )
    return (
        f"{sale_status}. Half-price about every {mean_w:.0f} weeks on average over "
        f"{cycle_count} cycles (±{stddev_w:.1f} weeks). Predicted next window: "
        f"{win_start.isoformat()} to {win_end.isoformat()}."
    )


def _today() -> date:
    return date.today()


def _predict_for_product(
    intervals_weeks: list[int],
    last_sale: date,
    *,
    today: date,
    min_cycles: int,
) -> tuple[float, float, date, date] | None:
    """Return (mean, stddev, window_start, window_end) or None if gated out."""
    n = len(intervals_weeks)
    if n < min_cycles:
        return None
    mean_w = statistics.fmean(intervals_weeks)
    if n >= 2:
        stddev_w = statistics.stdev(intervals_weeks)
        # Honest floor for low-cycle predictions where zero variance is a
        # coincidence of small N rather than real precision.
        if n < 4:
            stddev_w = max(stddev_w, STDDEV_FLOOR_WEEKS_LOW_N)
    else:
        stddev_w = STDDEV_FLOOR_WEEKS_N1
    half_width = min(stddev_w, MAX_WINDOW_HALFWIDTH_WEEKS)
    next_sale = last_sale + timedelta(weeks=mean_w)
    win_start = next_sale - timedelta(weeks=half_width)
    win_end = next_sale + timedelta(weeks=half_width)
    # Don't predict the past — if the predicted next-sale is already behind us,
    # roll forward by one mean-interval (the cycle has just slipped a beat).
    if win_end < today:
        next_sale = next_sale + timedelta(weeks=mean_w)
        win_start = next_sale - timedelta(weeks=half_width)
        win_end = next_sale + timedelta(weeks=half_width)
    return mean_w, stddev_w, win_start, win_end


def compute_predictions(
    specials: list[WeeklySpecial],
    *,
    min_cycles: int = DEFAULT_MIN_CYCLES,
    today: date | None = None,
    log: logging.Logger | None = None,
) -> tuple[list[Prediction], PredictionRunSummary]:
    """Group specials by product, compute interval stats, emit predictions.

    Each special currently on half-price contributes:
      - One observation (this week's sale at `week_start`)
      - One interval (last_halfprice_weeks_ago, if present)

    When we have multi-scrape history (Phase 1b), the input grows and intervals
    accumulate naturally. The same function works.
    """
    log = log or logging.getLogger("wednesday")
    today = today or _today()
    started = datetime.now(timezone.utc)

    # Group: (retailer, name) -> [(sale_date, interval_weeks_to_prior_sale_or_None)]
    by_product: dict[tuple[Retailer, str], list[tuple[date, int | None]]] = defaultdict(list)
    for s in specials:
        if not s.is_half_price:
            continue
        by_product[_key(s)].append((s.week_start, s.last_halfprice_weeks_ago))

    predictions: list[Prediction] = []
    gated_out = 0
    now = datetime.now(timezone.utc)

    for (retailer, name), entries in by_product.items():
        intervals = _derive_intervals(entries)
        last_sale = max(d for (d, _w) in entries)
        result = _predict_for_product(intervals, last_sale, today=today, min_cycles=min_cycles)
        if result is None:
            gated_out += 1
            continue
        mean_w, stddev_w, win_start, win_end = result
        # Confidence: more cycles = better, less dispersion = better.
        cycle_score = min(len(intervals) / 8.0, 1.0)
        dispersion_score = 1.0 - min(stddev_w / max(mean_w, 1.0), 1.0)
        confidence = round(0.5 * cycle_score + 0.5 * dispersion_score, 2)
        predictions.append(Prediction(
            retailer=retailer,
            product_name=name,
            predicted_window_start=win_start,
            predicted_window_end=win_end,
            confidence=confidence,
            confidence_tier=_confidence_tier(confidence),
            method="statistical",
            mean_interval_weeks=round(mean_w, 2),
            stddev_weeks=round(stddev_w, 2),
            cycle_count=len(intervals),
            last_sale_observed=last_sale,
            computed_at=now,
            rationale=_build_rationale(
                cycle_count=len(intervals),
                mean_w=mean_w,
                stddev_w=stddev_w,
                last_sale=last_sale,
                win_start=win_start,
                win_end=win_end,
                today=today,
            ),
        ))

    summary = PredictionRunSummary(
        computed_at=started,
        method="statistical",
        inputs_considered=len(by_product),
        predictions_emitted=len(predictions),
        gated_out=gated_out,
        duration_ms=int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
    )
    log.info(
        "predict.computed",
        extra={
            "inputs": summary.inputs_considered,
            "emitted": summary.predictions_emitted,
            "gated_out": summary.gated_out,
            "duration_ms": summary.duration_ms,
        },
    )
    return predictions, summary


def _resolve_input_path(pattern: str) -> Path:
    """Resolve a single input path, with glob fallback so users can pass wildcards."""
    if "*" in pattern or "?" in pattern:
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"No files match {pattern}")
        return Path(matches[-1])  # latest by name (timestamp prefix sorts correctly)
    p = Path(pattern)
    if not p.exists():
        raise FileNotFoundError(pattern)
    return p


def _maybe_load_dotenv() -> None:
    """Load .env from cwd or repo root so SUPABASE_DB_URL is picked up."""
    import os
    from pathlib import Path
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent.parent / ".env"]
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


def main(argv: list[str] | None = None) -> int:
    import os
    parser = argparse.ArgumentParser(
        prog="wednesday-prediction-statistical",
        description="Statistical cycle predictor (v1). Reads scraper JSON OR live Supabase, emits predictions JSON OR writes to predictions table.",
    )
    parser.add_argument("input", nargs="?", help="Path or glob to a scraper-produced JSON dump. Omit when using --from-db.")
    parser.add_argument("--from-db", action="store_true",
                        help="Read specials from Supabase instead of a JSON file (uses SUPABASE_DB_URL).")
    parser.add_argument("--write-db", action="store_true",
                        help="Write predictions to Supabase predictions table (uses SUPABASE_DB_URL).")
    parser.add_argument("--output", metavar="DIR", help="Directory to write predictions JSON (created if missing)")
    parser.add_argument("--min-cycles", type=int, default=DEFAULT_MIN_CYCLES,
                        help=f"Minimum historical intervals to emit a prediction (default {DEFAULT_MIN_CYCLES})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args(argv)

    log = configure_logging(verbose=args.verbose)

    if args.from_db or args.write_db:
        _maybe_load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL") if (args.from_db or args.write_db) else None
    if (args.from_db or args.write_db) and not db_url:
        log.error("SUPABASE_DB_URL not set; required for --from-db / --write-db")
        return 2

    if args.from_db:
        # Lazy import so the JSON-only path doesn't require psycopg.
        from src.db.reader import load_specials_from_db
        specials = load_specials_from_db(db_url, log)
        log.info("predict.start source=db min_cycles=%d", args.min_cycles)
    else:
        if not args.input:
            log.error("Either INPUT path or --from-db is required.")
            return 2
        try:
            input_path = _resolve_input_path(args.input)
        except FileNotFoundError as e:
            log.error("predict.input_missing pattern=%s error=%s", args.input, e)
            return 2
        log.info("predict.start source=%s min_cycles=%d", input_path, args.min_cycles)
        specials = _load_specials_from_json(input_path, log)

    if not specials:
        log.error("predict.no_specials_in_input")
        return 1

    predictions, summary = compute_predictions(specials, min_cycles=args.min_cycles, log=log)

    # Compact terminal preview — most actionable items first.
    print(f"\nTop 10 highest-confidence predictions:")
    sorted_preds = sorted(predictions, key=lambda p: (-p.confidence, p.predicted_window_start))
    for p in sorted_preds[:10]:
        name = (p.product_name[:42] + "...") if len(p.product_name) > 42 else p.product_name
        print(
            f"  [{p.retailer[:4]:<4}] {name:<45} "
            f"-> {p.predicted_window_start.isoformat()}..{p.predicted_window_end.isoformat()} "
            f"({p.confidence_tier:<6} {p.confidence:.2f}, n={p.cycle_count})"
        )

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"predictions_statistical_{ts}.json"
        payload = {
            "summary": summary.to_dict(),
            "predictions": [p.to_dict() for p in predictions],
        }
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("predict.json_written", extra={"path": str(out_path)})

    if args.write_db:
        from src.db.writer import write_predictions_to_db
        result = write_predictions_to_db(predictions, db_url=db_url, log=log)
        log.info(
            "predict.db_written inserted=%d unmatched=%d",
            result.inserted, result.skipped_unmatched,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
