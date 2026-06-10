"""Walk-forward backtest of the cycle predictor against real history.

The wedge claim is "we predict when products go half-price". This measures
it: for every product with enough half-price history, walk its sale dates
chronologically and, at each event, recompute the prediction the app WOULD
have shown the day after the previous sale (same algorithm, same honesty
gates — reusing the predictor's own functions, so the backtest can't drift
from production behavior). A window is a HIT when the next real sale lands
inside it.

Per product we keep totals plus the last-6 tested windows (what the app
shows as the track record); globally we aggregate hit rates per confidence
tier, which is the published "X% of high-confidence calls landed" number.

Methodology notes (honesty):
  * Each tested window is the FIRST window predicted after a sale — we do
    not give the model credit for rolled-forward re-predictions.
  * Events closer than MIN_CYCLE_INTERVAL_WEEKS to the prior sale are
    promo continuations, not cycle events — they're neither tested nor
    counted, exactly mirroring the predictor's own interval filter.
  * The tier recorded for a window is the tier AT PREDICTION TIME (with
    the warming-up cap), not the product's current tier.

CLI:
    python -m src.backtest --verbose              # report only (read-only)
    python -m src.backtest --write-db --verbose   # also persist to
        prediction_accuracy + accuracy_stats (migration 0020)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg

from src.env import load_dotenv
from src.prediction.statistical import (
    MAX_CONFIDENCE,
    MIN_CYCLE_INTERVAL_WEEKS,
    _confidence_tier,
    _derive_intervals,
    _predict_for_product,
)
from src.scrapers.base import configure_logging

# A product needs at least 3 cycle-distinct sale dates to test one window
# (two to form the first interval, a third to score the prediction).
MIN_SALE_DATES = 3

_PRODUCT_BATCH = 500


@dataclass
class WindowTest:
    predicted_start: date
    predicted_end: date
    actual: date
    tier: str
    hit: bool


@dataclass
class ProductAccuracy:
    product_id: str
    retailer: str
    name: str
    tests: list[WindowTest]

    @property
    def tested(self) -> int:
        return len(self.tests)

    @property
    def hits(self) -> int:
        return sum(1 for t in self.tests if t.hit)

    @property
    def last6(self) -> list[WindowTest]:
        return self.tests[-6:]


def _confidence_for(intervals: list[int], mean_w: float, stddev_w: float) -> tuple[float, str]:
    """Confidence + tier exactly as compute_predictions derives them."""
    cycle_score = min(len(intervals) / 8.0, 1.0)
    dispersion_score = 1.0 - min(stddev_w / max(mean_w, 1.0), 1.0)
    confidence = round(min(MAX_CONFIDENCE, 0.5 * cycle_score + 0.5 * dispersion_score), 2)
    tier = _confidence_tier(confidence)
    # Warming-up honesty cap (mirrors compute_predictions).
    if len(intervals) < 3 and tier != "low":
        tier = "low"
    return confidence, tier


def _load_sale_dates(db_url: str, log: logging.Logger) -> dict[tuple[str, str, str], list[date]]:
    """{(product_id, retailer, name) -> sorted distinct half-price week_starts}."""
    by_product: dict[tuple[str, str, str], list[date]] = defaultdict(list)
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select p.id::text, p.retailer, p.name, s.week_start
            from specials s
            join products p on p.id = s.product_id
            where s.is_half_price
            order by p.id, s.week_start
            """,
        )
        for pid, retailer, name, week_start in cur.fetchall():
            by_product[(pid, retailer, name)].append(week_start)
    log.info("backtest.loaded products=%d", len(by_product))
    return by_product


def backtest_product(dates: list[date]) -> list[WindowTest]:
    """Walk one product's sale dates; score every testable window."""
    tests: list[WindowTest] = []
    for k in range(2, len(dates)):
        history = dates[:k]
        actual = dates[k]
        # Promo continuation, not a cycle event — mirror the interval filter.
        if (actual - history[-1]).days < MIN_CYCLE_INTERVAL_WEEKS * 7:
            continue
        intervals = _derive_intervals([(d, None) for d in history])
        if not intervals:
            continue
        last_sale = history[-1]
        as_of = last_sale + timedelta(days=1)
        result = _predict_for_product(intervals, last_sale, today=as_of, min_cycles=1)
        if result is None:
            continue
        mean_w, stddev_w, win_start, win_end = result
        _conf, tier = _confidence_for(intervals, mean_w, stddev_w)
        tests.append(WindowTest(
            predicted_start=win_start,
            predicted_end=win_end,
            actual=actual,
            tier=tier,
            hit=win_start <= actual <= win_end,
        ))
    return tests


def run_backtest(db_url: str, *, log: logging.Logger) -> list[ProductAccuracy]:
    by_product = _load_sale_dates(db_url, log)
    results: list[ProductAccuracy] = []
    for (pid, retailer, name), dates in by_product.items():
        if len(dates) < MIN_SALE_DATES:
            continue
        tests = backtest_product(dates)
        if tests:
            results.append(ProductAccuracy(pid, retailer, name, tests))
    return results


def summarize(results: list[ProductAccuracy], log: logging.Logger) -> dict[str, tuple[int, int]]:
    """Aggregate {tier -> (tested, hits)} incl. 'overall'; print the report."""
    tiers: dict[str, tuple[int, int]] = {}
    for tier in ("high", "medium", "low"):
        tier_tests = [t for r in results for t in r.tests if t.tier == tier]
        tiers[tier] = (len(tier_tests), sum(1 for t in tier_tests if t.hit))
    all_tests = [t for r in results for t in r.tests]
    tiers["overall"] = (len(all_tests), sum(1 for t in all_tests if t.hit))

    print(f"\nBacktest: {len(results)} products, {len(all_tests)} predicted windows tested")
    print(f"{'tier':<10} {'windows':>8} {'hits':>7} {'hit rate':>9}")
    for tier in ("high", "medium", "low", "overall"):
        tested, hits = tiers[tier]
        rate = f"{100 * hits / tested:.1f}%" if tested else "—"
        print(f"{tier:<10} {tested:>8} {hits:>7} {rate:>9}")
    return tiers


def write_accuracy(
    db_url: str,
    results: list[ProductAccuracy],
    tiers: dict[str, tuple[int, int]],
    *,
    log: logging.Logger,
) -> None:
    """Replace prediction_accuracy + accuracy_stats with this run's numbers."""
    now = datetime.now(timezone.utc)
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute("delete from prediction_accuracy")
            for i in range(0, len(results), _PRODUCT_BATCH):
                batch = results[i:i + _PRODUCT_BATCH]
                ph = ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(batch))
                flat: list = []
                for r in batch:
                    last6 = r.last6
                    flat.extend([
                        r.product_id, r.tested, r.hits,
                        len(last6), sum(1 for t in last6 if t.hit), now,
                    ])
                cur.execute(
                    f"""
                    insert into prediction_accuracy
                        (product_id, windows_tested, hits, last6_tested, last6_hits, computed_at)
                    values {ph}
                    """,
                    flat,
                )
            cur.execute("delete from accuracy_stats")
            for tier, (tested, hits) in tiers.items():
                cur.execute(
                    """
                    insert into accuracy_stats (tier, windows_tested, hits, computed_at)
                    values (%s, %s, %s, %s)
                    """,
                    (tier, tested, hits, now),
                )
        conn.commit()
    log.info("backtest.written products=%d tiers=%d", len(results), len(tiers))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backtest")
    parser.add_argument("--write-db", action="store_true",
                        help="Persist results to prediction_accuracy + accuracy_stats.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    results = run_backtest(db_url, log=log)
    tiers = summarize(results, log)
    if args.write_db:
        write_accuracy(db_url, results, tiers, log=log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
