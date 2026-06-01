"""Shared dataclasses used across scrapers, predictors, and the pipeline.

These are intentionally denormalised: a single WeeklySpecial carries enough
information to populate the products table + price_observations + specials
rows when the pipeline writes to Supabase. Until the DB project exists, the
records are written as JSON so the eventual write is a trivial mapping step.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

Retailer = Literal["coles", "woolworths"]
ScrapeSource = Literal[
    "stockup_post", "coles_catalogue", "woolies_catalogue", "stockup_sheet", "user_submission"
]
ScrapeStatus = Literal["success", "partial", "failed", "no_data"]
PredictionMethod = Literal["statistical", "prophet"]
ConfidenceTier = Literal["low", "medium", "high"]


@dataclass
class WeeklySpecial:
    """One row from a weekly half-price report.

    Maps onto products + price_observations + specials at write time.
    """
    retailer: Retailer
    product_name: str
    category: str
    regular_price_cents: int
    sale_price_cents: int
    discount_pct: int
    is_half_price: bool
    # Cycle hint from the source ("Last 1/2 Price Sale @ Retailer" column).
    # Parsed where possible; raw kept for audit.
    last_halfprice_raw: str
    last_halfprice_weeks_ago: int | None
    last_halfprice_retailer: Retailer | None
    week_start: date
    week_end: date
    source: ScrapeSource
    source_url: str
    scraped_at: datetime
    # Optional product image URL when the source provides one directly (e.g. the
    # Woolworths browse API returns it). Lets the writer persist images without a
    # separate name-search pass. Defaulted so existing constructors are unaffected.
    image_url: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["week_start"] = self.week_start.isoformat()
        d["week_end"] = self.week_end.isoformat()
        d["scraped_at"] = self.scraped_at.isoformat()
        return d


@dataclass
class ScrapeRun:
    """One ingestion attempt — maps to the scrape_runs table.

    Started as soon as the scraper picks up work; finalised on completion or
    failure. Keeps stable provenance even when the run produced no data.
    """
    source: ScrapeSource
    started_at: datetime
    status: ScrapeStatus = "failed"
    items_found: int = 0
    duration_ms: int | None = None
    finished_at: datetime | None = None
    error: str | None = None
    notes: str | None = None
    source_url: str | None = None

    def finalise(self, *, status: ScrapeStatus, items: int, error: str | None = None) -> None:
        self.status = status
        self.items_found = items
        self.finished_at = datetime.now(timezone.utc)
        self.duration_ms = int(
            (self.finished_at - self.started_at).total_seconds() * 1000
        )
        if error is not None:
            self.error = error

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at.isoformat()
        return d


@dataclass
class ScrapeOutput:
    """Bundled artifacts from one scrape run, ready for JSON dump or DB write."""
    run: ScrapeRun
    specials: list[WeeklySpecial] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run": self.run.to_dict(),
            "specials": [s.to_dict() for s in self.specials],
        }


@dataclass
class Prediction:
    """Cycle prediction for a single product.

    Maps onto the predictions table when DB write is wired. Until then,
    we key by (retailer, product_name) since product_ids don't exist yet.
    """
    retailer: Retailer
    product_name: str
    predicted_window_start: date
    predicted_window_end: date
    confidence: float            # 0.0 to 1.0
    confidence_tier: ConfidenceTier
    method: PredictionMethod
    mean_interval_weeks: float
    stddev_weeks: float
    cycle_count: int             # n historical intervals used
    last_sale_observed: date     # most recent half-price we saw
    computed_at: datetime
    rationale: str               # plain-English explanation for the prediction card

    def to_dict(self) -> dict:
        d = asdict(self)
        d["predicted_window_start"] = self.predicted_window_start.isoformat()
        d["predicted_window_end"] = self.predicted_window_end.isoformat()
        d["last_sale_observed"] = self.last_sale_observed.isoformat()
        d["computed_at"] = self.computed_at.isoformat()
        return d


@dataclass
class PredictionRunSummary:
    """One-row summary of a prediction batch — useful for logs + audit."""
    computed_at: datetime
    method: PredictionMethod
    inputs_considered: int       # how many distinct products we had data for
    predictions_emitted: int     # how many we actually produced (passed gates)
    gated_out: int               # inputs that didn't meet cycle_count gate
    duration_ms: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["computed_at"] = self.computed_at.isoformat()
        return d
