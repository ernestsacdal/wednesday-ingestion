"""Prediction v2 ("hazard_v2"): when does a product next go half-price?

Why a new model: scored as users saw them, the statistical model's windows
(mean +/- sd of past intervals) landed 40.5% of the time — below the naive
guess "sometime in the next k weeks" at the same width (46.4%). Half-price
gaps are multimodal (a big spike at 2 weeks, then 3-4, then a long tail), so
a symmetric window around the mean sits in the wrong place.

The model (pure stdlib):
  1. Sale RUNS from a consistent weekly series (src/prediction/series.py);
     a gap is start-to-start in promo weeks.
  2. A retailer-wide gap distribution by discrete Kaplan-Meier (the open
     current gap of every product counts as censored, so slow cyclers aren't
     under-represented), over 1..G_MAX weeks plus a tail mass (">52 weeks").
  3. Each product's own gap counts shrunk toward that distribution
     (Dirichlet: (counts + alpha * prior) / (n + alpha)).
  4. Conditional on the weeks elapsed since the last run started (and on
     no new start having happened since), the probability that the next run
     starts in each of the next HORIZON promo weeks — blended 80/20 with the
     product's recent weekly half-price frequency (last 26 weeks), which
     carries the "this product has been on special a lot lately" signal the
     gap history misses.
  5. The shown window is the best 1-3 consecutive weeks (4 only when it adds
     real mass to a weak 3-week window); its probability mass, calibrated
     per retailer by PAV on held-out outcomes, is the stored confidence.

Evaluation (--eval): a walk-forward replay. At each week t the model sees
only history up to t, predicts for products not half-price at t (as-shown
semantics), and is scored against what happened after t — alongside the
current statistical model (replayed on the same series) and the naive
same-width guess. Calibration is fitted on an earlier fold and scored on a
later holdout, so the gate never sees its own training outcomes.

    python -m src.prediction.hazard --eval               # gate report
    python -m src.prediction.hazard --eval --write-db    # + store results + calibration map
    python -m src.prediction.hazard --write-db           # production predictions
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.eval import calibrate
from src.prediction import series as ser
from src.prediction.statistical import _derive_intervals, _predict_for_product

G_MAX = 52
HORIZON = 12
ALPHA = 8.0                   # grid 1-16 on the 2026-09-26 walk-forward: best Brier/ECE
FREQ_MIX = 0.2                # grid 0-0.2: improves Brier and hit rate, both retailers
FREQ_WEEKS = 26
MIN_EMIT_CONFIDENCE = 0.10
STALE_AFTER_WEEKS = 52        # no prediction once the last run is older than this
METHOD = "hazard_v2"

# Walk-forward folds, in weeks before the last labelled week.
RESOLVE_WEEKS = HORIZON + 4   # every window (k <= 12, width <= 4) has elapsed
CALIB_WEEKS = 16
HOLDOUT_WEEKS = 20
REFIT_EVERY = 4


# ------------------------------------------------------------------ model

@dataclass(frozen=True)
class GapModel:
    pmf: tuple[float, ...]     # index g = gap in weeks (0 unused), g <= G_MAX
    tail: float                # P(gap > G_MAX)


def km_fit(complete: list[int], censored: list[int]) -> GapModel:
    """Discrete Kaplan-Meier gap distribution with right-censoring."""
    events = Counter(g for g in complete if g <= G_MAX)
    exits = Counter(min(g, G_MAX + 1) for g in complete)          # leaves the risk set after g
    exits.update(min(c, G_MAX) for c in censored)
    # at_risk[g] = observations whose value (complete gap or censoring time) >= g
    at_risk = [0] * (G_MAX + 2)
    running = 0
    for g in range(G_MAX + 1, 0, -1):
        running += exits.get(g, 0)
        at_risk[g] = running
    pmf = [0.0] * (G_MAX + 1)
    surv = 1.0
    for g in range(1, G_MAX + 1):
        n = at_risk[g]
        h = events.get(g, 0) / n if n else 0.0
        pmf[g] = surv * h
        surv *= 1.0 - h
    return GapModel(tuple(pmf), surv)


def posterior(gaps: list[int], prior: GapModel, alpha: float = ALPHA) -> GapModel:
    """Product gap distribution: its own counts shrunk toward the prior."""
    n = len(gaps)
    counts = Counter(g if g <= G_MAX else G_MAX + 1 for g in gaps)
    denom = n + alpha
    pmf = tuple((counts.get(g, 0) + alpha * prior.pmf[g]) / denom if g else 0.0
                for g in range(G_MAX + 1))
    return GapModel(pmf, (counts.get(G_MAX + 1, 0) + alpha * prior.tail) / denom)


def next_week_probs(model: GapModel, elapsed: int, ongoing: bool,
                    horizon: int = HORIZON) -> list[float]:
    """P(next run starts k weeks from now), k = 1..horizon, given no start yet.

    ``elapsed`` = weeks since the last run started. While that run is still
    on (``ongoing``) the next START needs a gap week first, so the earliest
    possible next start is two weeks after the current one.
    """
    min_gap = elapsed + (2 if ongoing else 1)
    surv = sum(model.pmf[g] for g in range(max(min_gap, 1), G_MAX + 1)) + model.tail
    if surv <= 0:
        return [0.0] * horizon
    out = []
    for k in range(1, horizon + 1):
        g = elapsed + k
        out.append(model.pmf[g] / surv if min_gap <= g <= G_MAX else 0.0)
    return out


def best_window(probs: list[float]) -> tuple[int, int, float]:
    """(first k, width, mass) of the shown window: narrowest that keeps most mass."""
    def best(width: int) -> tuple[float, int]:
        return max((sum(probs[i:i + width]), i) for i in range(len(probs) - width + 1))
    m3, i3 = best(3)
    for width in (1, 2):
        m, i = best(width)
        if m >= 0.85 * m3:
            return i + 1, width, m
    m4, i4 = best(4)
    if m3 < 0.5 and m4 - m3 >= 0.12:
        return i4 + 1, 4, m4
    return i3 + 1, 3, m3


@dataclass(frozen=True)
class HazardPrediction:
    first_week: int
    last_week: int
    raw: float
    gaps_n: int
    last_start: int
    median_gap: float
    sd_gap: float


def gap_summary(model: GapModel) -> tuple[float, float]:
    """Median and sd of the (non-tail) gap distribution, for the 'why' copy."""
    mass = sum(model.pmf)
    if mass <= 0:
        return 0.0, 0.0
    cum, median = 0.0, 0.0
    for g in range(1, G_MAX + 1):
        cum += model.pmf[g] / mass
        if cum >= 0.5:
            median = float(g)
            break
    mean = sum(g * model.pmf[g] for g in range(G_MAX + 1)) / mass
    var = sum((g - mean) ** 2 * model.pmf[g] for g in range(G_MAX + 1)) / mass
    return median, var ** 0.5


def recent_frequency(half: set[int], t: int) -> float:
    """Share of the last FREQ_WEEKS promo weeks (up to t) the product was half-price."""
    return sum(1 for w in range(t - FREQ_WEEKS + 1, t + 1) if w in half) / FREQ_WEEKS


def week_probs(starts: list[int], half: set[int], t: int, half_now: bool, prior: GapModel,
               alpha: float = ALPHA) -> tuple[list[float], list[int]] | None:
    """Per-week probabilities for weeks t+1..t+HORIZON, and the past run starts."""
    past = starts[:bisect_right(starts, t)]
    if not past or t - past[-1] > STALE_AFTER_WEEKS:
        return None
    gaps = [b - a for a, b in zip(past, past[1:])]
    probs = next_week_probs(posterior(gaps, prior, alpha), t - past[-1], half_now)
    f = recent_frequency(half, t)
    return [(1 - FREQ_MIX) * p + FREQ_MIX * f for p in probs], past


def fixed_window(probs: list[float], width: int) -> tuple[int, float]:
    """(first k, mass) of the best window of exactly ``width`` weeks."""
    width = min(width, len(probs))
    mass, i = max((sum(probs[i:i + width]), i) for i in range(len(probs) - width + 1))
    return i + 1, mass


def predict(starts: list[int], t: int, half_now: bool, prior: GapModel,
            alpha: float = ALPHA, half: set[int] | None = None) -> HazardPrediction | None:
    """Prediction as of promo week ``t`` from a product's run starts."""
    res = week_probs(starts, half or set(), t, half_now, prior, alpha)
    if res is None:
        return None
    probs, past = res
    gaps = [b - a for a, b in zip(past, past[1:])]
    post = posterior(gaps, prior, alpha)
    k, width, mass = best_window(probs)
    median, sd = gap_summary(post)
    return HazardPrediction(t + k, t + k + width - 1, mass, len(gaps), past[-1], median, sd)


def fit_priors(starts_by_pid: dict[str, list[int]], retailer_of: dict[str, str],
               t: int) -> dict[str, GapModel]:
    """Per-retailer KM priors from every product's history up to week ``t``."""
    complete: dict[str, list[int]] = defaultdict(list)
    censored: dict[str, list[int]] = defaultdict(list)
    for pid, starts in starts_by_pid.items():
        past = starts[:bisect_right(starts, t)]
        if not past:
            continue
        r = retailer_of.get(pid)
        complete[r].extend(b - a for a, b in zip(past, past[1:]))
        censored[r].append(t - past[-1])
    return {r: km_fit(complete[r], censored[r]) for r in complete}


# ------------------------------------------------------------- evaluation

@dataclass(frozen=True)
class Case:
    pid: str
    retailer: str
    t: int
    raw: float
    hit: bool
    width: int
    naive_hit: bool
    b0_hit: bool | None
    b0_width: int | None
    freq_p: float
    v2_at_b0_width_hit: bool | None = None     # v2's best window of B0's exact width
    first_week: int | None = None


def _b0_window(half: set[int], t: int) -> tuple[int, int] | None:
    """The current statistical model, replayed on the same series as of week t,
    restricted to weeks after t (what is still ahead when shown)."""
    dates = [(ser.week_date(w), None) for w in sorted(w for w in half if w <= t)]
    if not dates:
        return None
    res = _predict_for_product(_derive_intervals(dates), dates[-1][0],
                               today=ser.week_date(t), min_cycles=1)
    if res is None:
        return None
    _mean, _sd, ws, we = res
    first, last = max(ser.week_idx(ws), t + 1), ser.week_idx(we)
    return (first, last) if last >= first else None


def walk_forward(series: dict[str, set[int]], retailer_of: dict[str, str],
                 weeks: list[int], *, alpha: float = ALPHA,
                 log: logging.Logger | None = None) -> list[Case]:
    starts = {pid: ser.run_starts(s) for pid, s in series.items() if s}
    cases: list[Case] = []
    priors: dict[str, GapModel] = {}
    for n, t in enumerate(weeks):
        if n % REFIT_EVERY == 0:
            priors = fit_priors(starts, retailer_of, t)
        for pid, st in starts.items():
            half = series[pid]
            if t in half:                       # shown as "half-price today" instead
                continue
            r = retailer_of.get(pid)
            if r not in priors:
                continue
            res = week_probs(st, half, t, False, priors[r], alpha)
            if res is None:
                continue
            probs, _past = res
            pred = predict(st, t, False, priors[r], alpha, half)
            width = pred.last_week - pred.first_week + 1
            hit = any(w in half for w in range(pred.first_week, pred.last_week + 1))
            naive = any(w in half for w in range(t + 1, t + 1 + width))
            b0 = _b0_window(half, t)
            b0_hit = b0_width = v2_eq = None
            if b0 is not None:
                b0_hit = any(w in half for w in range(b0[0], b0[1] + 1))
                b0_width = b0[1] - b0[0] + 1
                k0, _m = fixed_window(probs, b0_width)
                v2_eq = any(w in half for w in range(t + k0, t + k0 + b0_width))
            f = recent_frequency(half, t)
            cases.append(Case(pid, r, t, pred.raw, hit, width, naive, b0_hit, b0_width,
                              1 - (1 - f) ** width, v2_eq, pred.first_week))
        if log and n % 4 == 0:
            log.info("hazard.walk_forward t=%s cases=%d", ser.week_date(t), len(cases))
    return cases


def product_track_records(cases: list[Case]) -> dict[str, tuple[int, int, int, int]]:
    """Per product: (windows, hits, last-6 windows, last-6 hits) over NON-overlapping
    successive predictions — the next one counted only after the previous window
    has ended — so the product page's dots are independent calls, not one call
    re-counted every week it was on screen."""
    by_pid: dict[str, list[Case]] = defaultdict(list)
    for c in cases:
        by_pid[c.pid].append(c)
    out = {}
    for pid, cs in by_pid.items():
        episodes, busy_until = [], -1
        for c in sorted(cs, key=lambda c: c.t):
            if c.t <= busy_until:
                continue
            episodes.append(c.hit)
            busy_until = (c.first_week or c.t) + c.width - 1
        last6 = episodes[-6:]
        out[pid] = (len(episodes), sum(episodes), len(last6), sum(last6))
    return out


def _ece_brier(pairs: list[tuple[float, bool]]) -> tuple[float, float]:
    if not pairs:
        return 0.0, 0.0
    bins: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for p, h in pairs:
        bins[min(int(p * 10), 9)].append((p, h))
    n = len(pairs)
    ece = sum(len(b) / n * abs(sum(h for _p, h in b) / len(b) - sum(p for p, _h in b) / len(b))
              for b in bins.values())
    brier = sum((p - h) ** 2 for p, h in pairs) / n
    return round(ece, 4), round(brier, 4)


def gate_report(cases: list[Case], calib_weeks: set[int], holdout_weeks: set[int]) -> dict:
    """Fit calibration on the earlier fold, score everything on the holdout."""
    report: dict = {"retailers": {}, "calibration": {}}
    for r in sorted({c.retailer for c in cases}):
        fit = [(c.raw, c.hit) for c in cases if c.retailer == r and c.t in calib_weeks]
        blocks = calibrate.pav_fit(fit)
        report["calibration"][r] = blocks
        hold = [c for c in cases if c.retailer == r and c.t in holdout_weeks]
        if not hold:
            continue
        n = len(hold)
        cal = [(calibrate.apply(blocks, c.raw), c.hit) for c in hold]
        ece, brier = _ece_brier(cal)
        _e, freq_brier = _ece_brier([(c.freq_p, c.hit) for c in hold])
        both = [c for c in hold if c.b0_hit is not None]
        rep = {
            "n": n,
            "hit_rate": round(sum(c.hit for c in hold) / n, 4),
            "mean_width": round(sum(c.width for c in hold) / n, 2),
            "naive_hit_rate": round(sum(c.naive_hit for c in hold) / n, 4),
            "ece": ece, "brier": brier, "freq_brier": freq_brier,
            "b0_n": len(both),
            "b0_hit_rate": round(sum(c.b0_hit for c in both) / len(both), 4) if both else None,
            "b0_mean_width": round(sum(c.b0_width for c in both) / len(both), 2) if both else None,
            "v2_hit_rate_at_b0_width": (round(sum(c.v2_at_b0_width_hit for c in both) / len(both), 4)
                                        if both else None),
        }
        # Gate revised 2026-09-26 (user decision): at equal width every model —
        # this one, the statistical model and the naive guess — lands within
        # ~1 point, so v2 must be NON-INFERIOR there (within 1pp) rather than
        # +5pp; it earns its place through calibration, sharper always-forward
        # windows and per-week probabilities. The lift is still reported.
        rep["lift_vs_b0_at_equal_width"] = (
            round(rep["v2_hit_rate_at_b0_width"] - rep["b0_hit_rate"], 4) if both else None)
        rep["gate"] = {
            "noninferior_to_b0_at_equal_width": bool(
                both and rep["v2_hit_rate_at_b0_width"] >= rep["b0_hit_rate"] - 0.01),
            "beats_naive_same_width": rep["hit_rate"] > rep["naive_hit_rate"],
            "ece_le_0_05": ece <= 0.05,
            "brier_beats_frequency": brier < freq_brier,
        }
        rep["gate"]["pass"] = all(rep["gate"].values())
        report["retailers"][r] = rep
    report["pass"] = bool(report["retailers"]) and all(
        v["gate"]["pass"] for v in report["retailers"].values())
    return report


# --------------------------------------------------------------- DB + CLI

def load_series(db_url: str, log: logging.Logger):
    """(series by product id, retailer by id, last_seen by id, today's week)."""
    import psycopg

    from src.scrapers.hotprices import _history_points, _is_marketplace, fetch_dump
    from src.weeks import sydney_today

    today_week = ser.week_idx(sydney_today())
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute("select id::text, retailer, retailer_sku, last_seen from products")
        rows = cur.fetchall()
        cur.execute("""select week_start, retailer_sku, is_half from truth_snapshots
                        where source = 'woolies_api'""")
        truth_rows = cur.fetchall()
    pid_by_sku = {sku: pid for pid, _r, sku, _ls in rows}
    retailer_of = {pid: r for pid, r, _sku, _ls in rows}
    last_seen = {pid: ls for pid, _r, _sku, ls in rows}

    series: dict[str, set[int]] = {}
    for retailer in ("coles", "woolworths"):
        for raw in fetch_dump(retailer, log=log):
            if _is_marketplace(raw, retailer):
                continue
            pid = pid_by_sku.get(f"{retailer}:{raw.get('id')}")
            if pid is None:
                continue
            weeks = ser.half_weeks(_history_points(raw), today_week=today_week)
            if weeks:
                series[pid] = weeks
    # Complete live Woolworths crawls decide their weeks.
    by_week: dict[date, dict[str, bool]] = defaultdict(dict)
    for wk, sku, is_half in truth_rows:
        by_week[wk][sku] = is_half
    woolies = {pid for pid, r in retailer_of.items() if r == "woolworths"}
    for wk, skus in by_week.items():
        if len(skus) < 1000:
            continue
        half = {pid_by_sku[s] for s, h in skus.items() if h and s in pid_by_sku}
        ser.overlay_truth(series, ser.week_idx(wk), half, woolies)
    series = {pid: s for pid, s in series.items() if s}
    log.info("hazard.series products=%d truth_weeks=%d", len(series),
             sum(1 for s in by_week.values() if len(s) >= 1000))
    return series, retailer_of, last_seen, today_week


def predictions_eval_owns_stats(cur) -> bool:
    """True once the as-shown scorer has enough v2 claims to publish its own rates."""
    from src.eval.predictions_eval import MIN_METHOD_CLAIMS
    cur.execute("""select n from prediction_eval
                    where eval_kind = 'as_shown' and method = %s
                      and retailer is null and bucket is null
                    order by computed_at desc limit 1""", (METHOD,))
    row = cur.fetchone()
    return bool(row and row[0] >= MIN_METHOD_CLAIMS)


def _fmt_report(report: dict) -> str:
    lines = []
    for r, rep in report["retailers"].items():
        lines.append(
            f"{r}: n={rep['n']} v2 hit={rep['hit_rate']:.3f} (width {rep['mean_width']}) "
            f"naive={rep['naive_hit_rate']:.3f} | at B0's width (n={rep['b0_n']}, "
            f"w {rep['b0_mean_width']}): v2={rep['v2_hit_rate_at_b0_width']} "
            f"vs B0={rep['b0_hit_rate']} | "
            f"ECE={rep['ece']} Brier={rep['brier']} vs freq {rep['freq_brier']} "
            f"| gate {rep['gate']}")
    lines.append(f"GATE PASS: {report['pass']}")
    return "\n".join(lines)


def run_eval(db_url: str, log: logging.Logger, *, write_db: bool, alpha: float = ALPHA) -> dict:
    series, retailer_of, _last_seen, today_week = load_series(db_url, log)
    last_t = today_week - RESOLVE_WEEKS
    holdout = list(range(last_t - HOLDOUT_WEEKS + 1, last_t + 1))
    calib = list(range(holdout[0] - CALIB_WEEKS, holdout[0]))
    cases = walk_forward(series, retailer_of, calib + holdout, alpha=alpha, log=log)
    report = gate_report(cases, set(calib), set(holdout))
    log.info("hazard.gate\n%s", _fmt_report(report))
    if write_db:
        import psycopg
        with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
            for r, blocks in report["calibration"].items():
                calibrate.store(cur, r, blocks, method=METHOD)
            # accuracy_stats (the product page's "calls like this one landed X%")
            # must describe the SERVED model: until enough v2 claims have been
            # scored as shown, publish v2's calibrated holdout by the app's
            # verdict thresholds. predictions_eval takes over once they have.
            from src.eval.predictions_eval import APP_BUCKETS, app_bucket
            holdout_set = set(holdout)
            scored = [(calibrate.apply(report["calibration"].get(c.retailer, []), c.raw), c.hit)
                      for c in cases if c.t in holdout_set]
            tiers = {name: [h for conf, h in scored if app_bucket(conf) == name]
                     for name, _floor in APP_BUCKETS}
            tiers["overall"] = [h for _conf, h in scored]
            if not predictions_eval_owns_stats(cur):
                cur.execute("delete from accuracy_stats")
                for tier, hits in tiers.items():
                    cur.execute("insert into accuracy_stats (tier, windows_tested, hits, computed_at) "
                                "values (%s, %s, %s, now())", (tier, len(hits), sum(hits)))
                log.info("hazard.accuracy_stats from holdout %s",
                         {t: f"{sum(h)}/{len(h)}" for t, h in tiers.items()})
            # The product page's "last 6" dots: v2's own replay.
            records = product_track_records(cases)
            now = datetime.now(timezone.utc)
            cur.execute("delete from prediction_accuracy")
            rows = [(pid, *rec, now) for pid, rec in records.items()]
            for i in range(0, len(rows), 1000):
                chunk = rows[i:i + 1000]
                cur.execute(
                    "insert into prediction_accuracy (product_id, windows_tested, hits, "
                    "last6_tested, last6_hits, computed_at) values "
                    + ",".join(["(%s::uuid,%s,%s,%s,%s,%s)"] * len(chunk)),
                    [v for r in chunk for v in r])
            log.info("hazard.track_records products=%d", len(rows))
            for r, rep in report["retailers"].items():
                cur.execute(
                    """insert into prediction_eval
                           (eval_kind, method, retailer, n, hit_rate, mean_width_weeks,
                            naive_hit_rate, ece, brier, details)
                       values ('holdout', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                    (METHOD, r, rep["n"], rep["hit_rate"], rep["mean_width"],
                     rep["naive_hit_rate"], rep["ece"], rep["brier"],
                     json.dumps({**rep, "holdout_weeks": [str(ser.week_date(holdout[0])),
                                                          str(ser.week_date(holdout[-1]))],
                                 "alpha": alpha})),
                )
            conn.commit()
    return report


def run_predict(db_url: str, log: logging.Logger, *, write_db: bool) -> int:
    import psycopg

    from src.db.writer import write_predictions_to_db
    from src.models import Prediction
    from src.prediction.statistical import tier_for

    series, retailer_of, last_seen, _today_week = load_series(db_url, log)
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        cal = calibrate.load_latest(cur, method=METHOD)
        cur.execute("select max(week_start) from specials")
        served = cur.fetchone()[0]
        cur.execute("""select product_id::text from specials
                        where week_start = %s and is_half_price""", (served,))
        half_now = {r[0] for r in cur.fetchall()}
    if not cal:
        log.error("hazard.no_calibration — run --eval --write-db first")
        return 2
    t = ser.week_idx(served)
    for pid in half_now:                         # the served week is authoritative
        series.setdefault(pid, set()).add(t)
    starts = {pid: ser.run_starts(s) for pid, s in series.items()}
    priors = fit_priors(starts, retailer_of, t)
    fresh_after = datetime.now(timezone.utc) - timedelta(days=14)
    now = datetime.now(timezone.utc)
    preds: list[Prediction] = []
    for pid, st in starts.items():
        r = retailer_of.get(pid)
        seen = last_seen.get(pid)
        if r not in priors or seen is None or seen < fresh_after:
            continue
        p = predict(st, t, pid in half_now, priors[r], half=series[pid])
        if p is None:
            continue
        conf = calibrate.apply(cal.get(r, []), p.raw)
        if conf < MIN_EMIT_CONFIDENCE:
            continue
        first, last = ser.week_date(p.first_week), ser.week_date(p.last_week) + timedelta(days=6)
        preds.append(Prediction(
            retailer=r, product_name="", predicted_window_start=first,
            predicted_window_end=last, confidence=conf, confidence_tier=tier_for(conf),
            method=METHOD, mean_interval_weeks=round(p.median_gap, 2),
            stddev_weeks=round(p.sd_gap, 2), cycle_count=p.gaps_n,
            last_sale_observed=ser.week_date(p.last_start), computed_at=now,
            rationale=(f"Typically half-price every ~{p.median_gap:.0f} weeks "
                       f"({p.gaps_n} gaps seen); next likely {first:%d %b}-{last:%d %b}."),
            product_id=pid, raw_confidence=round(p.raw, 3),
        ))
    confs = [p.confidence for p in preds]
    log.info("hazard.predict served=%s products=%d median_conf=%.2f tiers=%s", served,
             len(preds), statistics.median(confs) if confs else 0,
             dict(Counter(p.confidence_tier for p in preds)))
    if write_db:
        write_predictions_to_db(preds, db_url=db_url, log=log)
    return 0


def main(argv: list[str] | None = None) -> int:
    from src.env import load_dotenv
    from src.scrapers.base import configure_logging

    parser = argparse.ArgumentParser(prog="hazard")
    parser.add_argument("--eval", action="store_true", help="Walk-forward gate report.")
    parser.add_argument("--strict", action="store_true",
                        help="With --eval: exit 1 when the gate fails.")
    parser.add_argument("--alpha", type=float, default=ALPHA)
    parser.add_argument("--write-db", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)
    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2
    if args.eval:
        report = run_eval(db_url, log, write_db=args.write_db, alpha=args.alpha)
        if not report["pass"]:
            log.error("hazard.gate_failed — keep serving the previous run (rollback: "
                      "python -m src.prediction.statistical --from-db --write-db)")
        return 0 if report["pass"] or not args.strict else 1
    return run_predict(db_url, log, write_db=args.write_db)


if __name__ == "__main__":
    sys.exit(main())
