"""Phase 1a spike: discover whether Coles + Woolies publish accessible catalogue PDFs.

Background:
  - Phase 0 spike showed both retailers' specials SPAs are YELLOW
    (reachable but JS-rendered, would need Playwright + session handling).
  - The build plan calls for a direct-catalogue scraper as resilience-fallback
    to the StockUp OzBargain post (which is competitor-controlled).
  - Catalogue PDFs (often served via FlippingBook / Issuu / Publitas) are
    typically a more parseable surface than the customer-facing SPAs.

This spike:
  1. GETs each retailer's catalogue landing page(s)
  2. Greps for PDF URLs, flipbook viewer URLs, embedded JSON config keys
  3. Reports a per-retailer verdict
  4. Saves raw HTML to data/raw/ for manual inspection

Run:
    python -m src.scrapers.catalogue_pdf_spike
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from src.scrapers.base import build_session, configure_logging

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

PROBES: list[tuple[str, str, str]] = [
    # (name, retailer, url)
    ("coles-catalogues-landing", "coles", "https://www.coles.com.au/catalogues"),
    ("coles-catalogue-singular", "coles", "https://www.coles.com.au/catalogue"),
    ("coles-weekly-catalogue", "coles", "https://www.coles.com.au/catalogues/weekly"),
    ("woolies-catalogue", "woolworths", "https://www.woolworths.com.au/shop/catalogue"),
    ("woolies-specials-catalogue", "woolworths",
     "https://www.woolworths.com.au/shop/discover/specials/catalogue"),
    ("woolies-catalogues-listing", "woolworths", "https://www.woolworths.com.au/catalogues"),
]

# All PDF URLs in the HTML (case-insensitive .pdf endings, ignoring querystrings).
PDF_URL_PAT = re.compile(r"https?://[^\s\"'<>]+?\.pdf\b", re.IGNORECASE)

# Known flipbook / catalogue viewer platforms. We look for these because PDFs
# are often hidden behind a viewer rather than linked directly.
FLIPBOOK_HOST_PAT = re.compile(
    r"(flippingbook|issuu|publitas|ourcatalogue|catalogues?\.[a-z0-9-]+)\.[a-z.]{2,8}/[^\s\"'<>]*",
    re.IGNORECASE,
)

# Embedded JSON keys that often point to a downloadable PDF inside SPA bundles.
EMBEDDED_PDF_KEY_PAT = re.compile(
    r'["\'](?:pdf|document|catalogue|catalog)(?:Url|Path|Source)["\']\s*[:=]\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# A "catalogue-relevant" PDF is one whose URL hints it's a catalogue / specials,
# not a help doc or terms PDF. Keeps the noise down in the verdict.
CATALOGUE_HINT_PAT = re.compile(r"(catalogue|catalog|specials?|weekly|deal)", re.IGNORECASE)


@dataclass
class Finding:
    probe: str
    url: str
    status: int | None
    size_bytes: int
    pdfs: list[str] = field(default_factory=list)
    catalogue_pdfs: list[str] = field(default_factory=list)
    flipbooks: list[str] = field(default_factory=list)
    embedded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _trim(items: list[str], n: int = 5) -> list[str]:
    return list(dict.fromkeys(items))[:n]


def probe_one(session: requests.Session, name: str, url: str) -> Finding:
    print(f"\n--- {name} ---")
    print(f"  GET {url}")
    f = Finding(probe=name, url=url, status=None, size_bytes=0)

    try:
        resp = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        f.notes.append(f"request failed: {e}")
        print(f"  x {f.notes[-1]}")
        return f

    f.status = resp.status_code
    f.size_bytes = len(resp.content)
    body = resp.text
    print(f"  status={f.status}  size={f.size_bytes}b")

    if resp.status_code != 200:
        return f

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{name}.html").write_text(body, encoding="utf-8", errors="replace")

    f.pdfs = _trim(PDF_URL_PAT.findall(body), n=10)
    f.catalogue_pdfs = [u for u in f.pdfs if CATALOGUE_HINT_PAT.search(u)]
    f.flipbooks = _trim([m if isinstance(m, str) else m[0] for m in FLIPBOOK_HOST_PAT.findall(body)], n=10)
    f.embedded = _trim(EMBEDDED_PDF_KEY_PAT.findall(body), n=10)

    if f.catalogue_pdfs:
        print(f"  + catalogue-hint PDFs ({len(f.catalogue_pdfs)}):")
        for u in f.catalogue_pdfs[:5]:
            print(f"      {u[:120]}")
    elif f.pdfs:
        print(f"  ~ PDFs found but none look catalogue-related ({len(f.pdfs)} total, sample): {f.pdfs[0][:120]}")
    if f.flipbooks:
        print(f"  + flipbook/viewer signals ({len(f.flipbooks)}):")
        for u in f.flipbooks[:5]:
            print(f"      {u[:120]}")
    if f.embedded:
        print(f"  + embedded PDF/document keys ({len(f.embedded)}):")
        for u in f.embedded[:5]:
            print(f"      {u[:120]}")
    if not (f.catalogue_pdfs or f.flipbooks or f.embedded):
        print("  - no catalogue PDF / flipbook signals")

    return f


def verdict_for(findings: list[Finding]) -> tuple[str, str]:
    if any(f.catalogue_pdfs for f in findings):
        return "GREEN", "direct catalogue PDF URL discovered — parseable with pdfplumber"
    if any(f.embedded for f in findings):
        return "YELLOW", "embedded PDF/document key found in HTML — reachable with one extraction step"
    if any(f.flipbooks for f in findings):
        return "YELLOW", "flipbook viewer detected — would need to extract PDF URL from viewer config"
    if any(f.status == 200 for f in findings):
        return "RED", "pages reachable but no PDF/catalogue signals — try Playwright or fall back to alt OzBargain authors"
    return "RED", "no probe returned 200 — endpoints may have moved"


def main(argv: list[str] | None = None) -> int:
    log = configure_logging(verbose=False)
    session = build_session()

    print("=" * 72)
    print("Wednesday — Phase 1a spike: catalogue PDF discovery")
    print("=" * 72)

    by_retailer: dict[str, list[Finding]] = {"coles": [], "woolworths": []}
    for name, retailer, url in PROBES:
        f = probe_one(session, name, url)
        by_retailer[retailer].append(f)
        time.sleep(2)  # polite

    print("\n" + "=" * 72)
    print("VERDICTS")
    print("=" * 72)
    for retailer, fs in by_retailer.items():
        v, reason = verdict_for(fs)
        print(f"  {retailer:<11} {v:<7} — {reason}")
    print()
    print("GREEN  = direct catalogue PDF URL accessible, low-effort scrape")
    print("YELLOW = catalogue exists but extraction needs another step (or Playwright)")
    print("RED    = no catalogue surface found via simple HTTP — pivot needed")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
