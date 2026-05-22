"""Production scraper: weekly Catalogue Half-Price Report from OzBargain.

Source: posts by StockUpApp (user id 297137 — discovered in Phase 0 spike,
2026-05-12; the original spec claimed this was Samwise Gamgee, but that
was wrong). Each Wednesday they post a "Catalogue Half-Price Report" with
~180 products across Coles + Woolworths, in structured HTML tables that
include a "Last 1/2 Price Sale @ Retailer" cycle column.

Resolution strategy:
  1. Hit StockUpApp's user-page to find the latest post by them.
  2. Confirm it's a half-price catalogue post (title pattern + body structure).
  3. Parse week range + per-row specials from the tables.

Fallback: search OzBargain for the title pattern. Used if user page changes.

Output: ScrapeOutput with the audit ScrapeRun + list[WeeklySpecial].
The pipeline.py CLI decides what to do with it (JSON dump or DB write).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.models import (
    Retailer, ScrapeOutput, ScrapeRun, WeeklySpecial,
)
from src.scrapers.base import polite_get

BASE = "https://www.ozbargain.com.au"
STOCKUP_USER_ID = "297137"
STOCKUP_PROFILE_URL = f"{BASE}/user/{STOCKUP_USER_ID}"
SEARCH_FALLBACK_URL = f"{BASE}/search/node?keys=Catalogue+Half-Price+Report"

# Title patterns that identify a comprehensive catalogue post (not a single deal).
TITLE_PATTERNS = [
    re.compile(r"Catalogue\s+Half-Price\s+Report", re.IGNORECASE),
    re.compile(r"1/?2\s*price.*\+\s*\d+\s+more", re.IGNORECASE),
]


@dataclass
class _PostInfo:
    title: str
    url: str


def _to_cents(price: str) -> int:
    s = price.strip().replace("$", "").replace(",", "")
    return int(round(float(s) * 100))


def _pct_to_int(s: str) -> int:
    return int(s.strip().rstrip("%"))


def _parse_last_halfprice(raw: str) -> tuple[int | None, Retailer | None]:
    """Parse cells like '8 weeks ago @ CL' / '1 week ago @ WW' / '—'."""
    if not raw or raw.strip() in ("—", "-", "–"):
        return None, None
    m = re.match(r"(\d+)\s+weeks?\s+ago\s+@\s+(CL|WW)", raw.strip(), re.IGNORECASE)
    if not m:
        return None, None
    weeks = int(m.group(1))
    code = m.group(2).upper()
    return weeks, ("coles" if code == "CL" else "woolworths")


def _retailer_from_cell(cell: str) -> Retailer | None:
    s = cell.strip().lower()
    if "coles" in s:
        return "coles"
    if "woolworth" in s or s == "ww":
        return "woolworths"
    return None


def _find_post_via_user_page(
    session: requests.Session, log: logging.Logger
) -> _PostInfo | None:
    """Look on StockUpApp's user page for the latest catalogue post."""
    try:
        resp = polite_get(session, STOCKUP_PROFILE_URL, log=log)
    except requests.RequestException as e:
        log.warning("user_page.failed", extra={"error": str(e)})
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/node/" not in href:
            continue
        title = a.get_text(" ", strip=True)
        if not any(p.search(title) for p in TITLE_PATTERNS):
            continue
        full = urljoin(BASE, href)
        if full in seen:
            continue
        seen.add(full)
        return _PostInfo(title=title, url=full)
    return None


def _find_post_via_search(
    session: requests.Session, log: logging.Logger
) -> _PostInfo | None:
    """Fallback: text-search OzBargain for the title pattern."""
    try:
        resp = polite_get(session, SEARCH_FALLBACK_URL, log=log)
    except requests.RequestException as e:
        log.warning("search_fallback.failed", extra={"error": str(e)})
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(" ", strip=True)
        if "/node/" not in href:
            continue
        if not any(p.search(title) for p in TITLE_PATTERNS):
            continue
        return _PostInfo(title=title, url=urljoin(BASE, href))
    return None


def _parse_week_range(body: BeautifulSoup, log: logging.Logger) -> tuple[date, date]:
    """Pull week_start/week_end from the post intro.

    Preferred source: the H5 heading 'Catalogue Half-Price Report — YYYY-MM-DD'.
    Fallback: today's Wednesday (most recent Wed not after today).
    """
    for h5 in body.find_all("h5"):
        t = h5.get_text(" ", strip=True)
        if "Catalogue Half-Price Report" not in t:
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})", t)
        if m:
            start = date.fromisoformat(m.group(1))
            return start, start + timedelta(days=6)

    log.warning("week_range.h5_missing, falling back to most recent Wednesday")
    today = date.today()
    days_since_wed = (today.weekday() - 2) % 7  # Mon=0, Wed=2
    start = today - timedelta(days=days_since_wed)
    return start, start + timedelta(days=6)


def _parse_specials_from_body(
    body: BeautifulSoup,
    *,
    week_start: date,
    week_end: date,
    source_url: str,
    scraped_at: datetime,
    log: logging.Logger,
) -> list[WeeklySpecial]:
    """Walk h5 + table.data pairs, emit a WeeklySpecial per data row.

    Dedup applied at the end with key (retailer, product_name, sale_price_cents).
    """
    specials: list[WeeklySpecial] = []
    current_category = "Uncategorised"
    skipped = 0

    for el in body.find_all(["h5", "table"]):
        if el.name == "h5":
            text = el.get_text(" ", strip=True)
            # Skip the report-header h5; that's metadata, not a category.
            if "Catalogue Half-Price Report" in text:
                continue
            current_category = text or "Uncategorised"
            continue

        if "data" not in (el.get("class") or []):
            continue

        for tr in el.select("tbody > tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 6:
                skipped += 1
                continue
            retailer = _retailer_from_cell(cells[0])
            if retailer is None:
                skipped += 1
                continue
            try:
                regular = _to_cents(cells[2])
                sale = _to_cents(cells[4])
                pct = _pct_to_int(cells[5])
            except (ValueError, IndexError):
                skipped += 1
                continue
            last_raw = cells[6] if len(cells) > 6 else ""
            weeks_ago, last_retailer = _parse_last_halfprice(last_raw)
            specials.append(WeeklySpecial(
                retailer=retailer,
                product_name=cells[1],
                category=current_category,
                regular_price_cents=regular,
                sale_price_cents=sale,
                discount_pct=pct,
                is_half_price=pct >= 50,
                last_halfprice_raw=last_raw,
                last_halfprice_weeks_ago=weeks_ago,
                last_halfprice_retailer=last_retailer,
                week_start=week_start,
                week_end=week_end,
                source="stockup_post",
                source_url=source_url,
                scraped_at=scraped_at,
            ))

    # Dedup identical rows that StockUp sometimes emits twice (same product /
    # retailer / sale price). Keep first occurrence.
    seen: set[tuple] = set()
    deduped: list[WeeklySpecial] = []
    for s in specials:
        key = (s.retailer, s.product_name, s.sale_price_cents)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)

    if skipped:
        log.debug("parse.skipped_rows", extra={"count": skipped})
    if len(deduped) < len(specials):
        log.info("parse.deduped", extra={"original": len(specials), "deduped": len(deduped)})

    return deduped


def scrape(session: requests.Session, log: logging.Logger) -> ScrapeOutput:
    """Top-level entry: find latest StockUp post, parse it, return ScrapeOutput."""
    started = datetime.now(timezone.utc)
    run = ScrapeRun(source="stockup_post", started_at=started)

    log.info("scrape.start", extra={"source": "stockup_post"})

    post = _find_post_via_user_page(session, log)
    if post is None:
        log.info("user_page.no_match, trying search fallback")
        post = _find_post_via_search(session, log)

    if post is None:
        run.finalise(status="no_data", items=0, error="No catalogue post found via user page or search")
        log.error("scrape.no_post_found")
        return ScrapeOutput(run=run)

    run.source_url = post.url
    log.info("scrape.post_selected", extra={"title": post.title[:90], "url": post.url})

    try:
        resp = polite_get(session, post.url, log=log)
    except requests.RequestException as e:
        run.finalise(status="failed", items=0, error=f"Fetch failed: {e}")
        log.exception("scrape.fetch_failed")
        return ScrapeOutput(run=run)

    soup = BeautifulSoup(resp.text, "lxml")
    node = soup.find("div", class_=re.compile(r"\bnode-ozbdeal\b"))
    if node is None:
        run.finalise(status="failed", items=0, error="Post body node not found (selector drift?)")
        log.error("scrape.body_node_missing")
        return ScrapeOutput(run=run)

    body = node.find("div", class_=re.compile(r"\bcontent\b"))
    if body is None:
        run.finalise(status="failed", items=0, error="Content div not found inside post node")
        log.error("scrape.content_div_missing")
        return ScrapeOutput(run=run)

    week_start, week_end = _parse_week_range(body, log)
    scraped_at = datetime.now(timezone.utc)
    specials = _parse_specials_from_body(
        body,
        week_start=week_start,
        week_end=week_end,
        source_url=post.url,
        scraped_at=scraped_at,
        log=log,
    )

    if not specials:
        run.finalise(status="no_data", items=0, error="Post fetched but parsed zero specials")
        log.error("scrape.zero_specials")
        return ScrapeOutput(run=run)

    run.finalise(status="success", items=len(specials))
    log.info(
        "scrape.success",
        extra={
            "items": len(specials),
            "coles": sum(1 for s in specials if s.retailer == "coles"),
            "woolies": sum(1 for s in specials if s.retailer == "woolworths"),
            "with_cycle": sum(1 for s in specials if s.last_halfprice_weeks_ago is not None),
            "week_start": week_start.isoformat(),
            "duration_ms": run.duration_ms,
        },
    )
    return ScrapeOutput(run=run, specials=specials)
