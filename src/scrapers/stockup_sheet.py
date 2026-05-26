"""Production scraper: StockUp's linked Google Sheet.

Source: the public Google Sheet linked from StockUp's weekly OzBargain
post. Discovered in Phase 0 (2026-05-12) but not pursued until the
Phase A data-completeness push (2026-05-22 onward).

The sheet is much richer than the weekly post — ~2,600 products at
45%+ off across Coles + Woolworths, vs ~180 curated rows in the post.
Same author as the post so curation conventions are consistent.

Output: ScrapeOutput with audit + list[WeeklySpecial] tagged
`source="stockup_sheet"`. Caller composes this with the post output
(see pipeline.py) — post rows win on `(retailer, name)` collisions
because they carry richer per-row cycle metadata.

Sheet layout (after skipping a marketing banner on row 0, header on row 1):
  Retailer, Name, Category, Was Price, Discount ($), Price Now,
  Discount %, Last 45%+ Off, Weeks Ago @ Retailer

`Discount ($)` is the amount off in dollars (NOT the sale price).
`Price Now` is the sale price.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone

import requests

from src.models import Retailer, ScrapeOutput, ScrapeRun, WeeklySpecial
from src.scrapers.base import polite_get

SHEET_ID = "1v2rVFsDTNmp9TC3rpGawt_oEcEWriqxxXyueOTWAFp4"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

# Defensive cap — if the sheet ever explodes past this, log + truncate.
MAX_ROWS = 5000


_PRICE_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")


def _parse_dollars_to_cents(raw: str) -> int | None:
    """Parse ' $ 3.50 ' style strings to cents (350). None on failure."""
    if not raw:
        return None
    m = _PRICE_RE.search(raw)
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", "")) * 100))
    except ValueError:
        return None


def _parse_pct_to_int(raw: str) -> int | None:
    """Parse '75.14%' style strings to int (75)."""
    if not raw:
        return None
    s = raw.strip().rstrip("%")
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _parse_last_halfprice(raw: str) -> tuple[int | None, Retailer | None]:
    """Parse 'X weeks ago @ CL'/'@ WW' to (weeks_ago, retailer)."""
    if not raw or raw.strip() in ("—", "-", "–", ""):
        return None, None
    m = re.match(r"(\d+)\s+weeks?\s+ago\s+@\s+(CL|WW)", raw.strip(), re.IGNORECASE)
    if not m:
        return None, None
    weeks = int(m.group(1))
    code = m.group(2).upper()
    return weeks, ("coles" if code == "CL" else "woolworths")


def _retailer_from_cell(cell: str) -> Retailer | None:
    s = cell.strip().lower()
    if s == "coles":
        return "coles"
    if s in ("woolworths", "woolies"):
        return "woolworths"
    return None


def _most_recent_wednesday(today: date | None = None) -> date:
    """Return the most recent Wednesday on or before today.

    The sheet doesn't carry a per-row week_start — it's a snapshot of
    'currently on sale'. We anchor everything to the most recent Wed so
    sheet rows align with post rows + the predictor's weekly cadence.
    """
    today = today or date.today()
    # Mon=0, Tue=1, Wed=2, ...
    days_since_wed = (today.weekday() - 2) % 7
    return today - timedelta(days=days_since_wed)


def scrape(session: requests.Session, log: logging.Logger) -> ScrapeOutput:
    """Top-level entry: fetch the sheet, parse rows, return ScrapeOutput."""
    started = datetime.now(timezone.utc)
    run = ScrapeRun(source="stockup_sheet", started_at=started, source_url=CSV_URL)

    log.info("scrape.start", extra={"source": "stockup_sheet"})

    try:
        resp = polite_get(session, CSV_URL, log=log)
    except requests.RequestException as e:
        run.finalise(status="failed", items=0, error=f"Fetch failed: {e}")
        log.exception("sheet.fetch_failed")
        return ScrapeOutput(run=run)

    text = resp.text
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        run.finalise(status="no_data", items=0, error="Sheet has fewer than 3 rows")
        log.error("sheet.too_few_rows", extra={"rows": len(rows)})
        return ScrapeOutput(run=run)

    # Row 0 = marketing banner. Row 1 = header. Data starts at row 2.
    header = [c.strip() for c in rows[1]]
    expected = ["Retailer", "Name", "Category"]
    if header[:3] != expected:
        run.finalise(
            status="failed",
            items=0,
            error=f"Header drift; expected starts with {expected}, got {header[:3]}",
        )
        log.error("sheet.header_drift", extra={"header": header})
        return ScrapeOutput(run=run)

    week_start = _most_recent_wednesday()
    week_end = week_start + timedelta(days=6)
    scraped_at = datetime.now(timezone.utc)

    specials: list[WeeklySpecial] = []
    skipped = 0
    truncated = False

    for raw in rows[2:]:
        if len(specials) >= MAX_ROWS:
            truncated = True
            break
        if len(raw) < 9:
            skipped += 1
            continue
        retailer = _retailer_from_cell(raw[0])
        name = raw[1].strip()
        category = raw[2].strip() or "Uncategorised"
        regular = _parse_dollars_to_cents(raw[3])
        sale = _parse_dollars_to_cents(raw[5])
        pct = _parse_pct_to_int(raw[6])
        last_raw = raw[8] if len(raw) > 8 else ""

        if not retailer or not name or regular is None or sale is None or pct is None:
            skipped += 1
            continue
        # Sanity: sale should be < regular. Reject obvious mis-parses.
        if sale >= regular:
            skipped += 1
            continue

        weeks_ago, last_retailer = _parse_last_halfprice(last_raw)
        specials.append(
            WeeklySpecial(
                retailer=retailer,
                product_name=name,
                category=category,
                regular_price_cents=regular,
                sale_price_cents=sale,
                discount_pct=pct,
                is_half_price=pct >= 50,
                last_halfprice_raw=last_raw,
                last_halfprice_weeks_ago=weeks_ago,
                last_halfprice_retailer=last_retailer,
                week_start=week_start,
                week_end=week_end,
                source="stockup_sheet",
                source_url=CSV_URL,
                scraped_at=scraped_at,
            )
        )

    if skipped:
        log.info("sheet.skipped_rows", extra={"count": skipped})
    if truncated:
        log.warning("sheet.truncated_at_max", extra={"max": MAX_ROWS})

    if not specials:
        run.finalise(status="no_data", items=0, error="Parsed zero usable rows")
        log.error("sheet.zero_usable")
        return ScrapeOutput(run=run)

    run.finalise(status="success", items=len(specials))
    log.info(
        "sheet.success",
        extra={
            "items": len(specials),
            "coles": sum(1 for s in specials if s.retailer == "coles"),
            "woolies": sum(1 for s in specials if s.retailer == "woolworths"),
            "half_price": sum(1 for s in specials if s.is_half_price),
            "week_start": week_start.isoformat(),
            "duration_ms": run.duration_ms,
        },
    )
    return ScrapeOutput(run=run, specials=specials)


if __name__ == "__main__":
    # Standalone smoke test — run with: python -m src.scrapers.stockup_sheet
    from src.scrapers.base import build_session, configure_logging

    log = configure_logging(verbose=False)
    session = build_session()
    output = scrape(session, log)
    print(f"\nstatus={output.run.status}  items={output.run.items_found}")
    if output.specials:
        print("First 3:")
        for s in output.specials[:3]:
            print(
                f"  [{s.retailer[:4]}] {s.discount_pct:>3}% "
                f"${s.regular_price_cents/100:>6.2f} -> ${s.sale_price_cents/100:>6.2f}  "
                f"{s.product_name[:60]}"
            )
        half = sum(1 for s in output.specials if s.is_half_price)
        print(f"\nHalf-price rows: {half} / {len(output.specials)}")
