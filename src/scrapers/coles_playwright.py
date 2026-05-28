"""Coles direct image discovery via Playwright (headless Chromium).

Why this exists: the Woolworths internal API (src/scrapers/product_images.py)
covers ~84% of products either directly or via cross-retailer match. The
remaining ~16% is dominated by Coles house brands (Coles Bakery, Farmers
Market, Red Tractor) and niche imports (Chick-Fil-A, HBAF) that don't
exist at Woolies. To get those, we need to scrape Coles directly.

Coles' search page is a JS-rendered Next.js SPA behind Cloudflare. Raw
HTTP returns either a 4KB shell or a challenge page. Playwright with a
real browser fingerprint gets through reliably.

Strategy per (product_name):
  1. Navigate to /search/products?q=<name>
  2. Wait for product cards to render (~3-5s)
  3. Pick the first card whose name token-overlaps the query >=60%
  4. Either extract the card's <img> src directly OR navigate to the
     product page + read og:image
  5. Return ImageLookupResult mirroring the Woolies-API surface

This module is **local-only**. It is not loaded by the weekly GHA cron
(Chromium isn't installed in that environment). Run it manually via
src/backfill_coles_images.py when image coverage looks stale.

Public surface:
    async lookup_coles_image(context, product_name, log) -> ImageLookupResult
    async build_browser_context() -> AsyncContextManager[BrowserContext]
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.scrapers.product_images import ImageLookupResult

# Realistic Chrome 120 fingerprint. Coles' Cloudflare lets these through.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_COLES_SEARCH = "https://www.coles.com.au/search/products?q={q}"
_COLES_ORIGIN = "https://www.coles.com.au"

# Match heuristic (same as Woolies adapter): >=60% query-token overlap.
_MIN_TOKEN_OVERLAP = 0.6

# Per-request budgets.
_PAGE_LOAD_TIMEOUT_MS = 30_000
_PRODUCT_TILE_TIMEOUT_MS = 12_000

_NORMALISE_RE = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE_RE = re.compile(r"\s+")

# StockUp catalogue rows often bundle multiple variants into one entry,
# e.g. "Pantene Miracles Shampoo 650mL or Conditioner 600mL" or
# "Cadbury Bites or Balls 120g-142g". Coles' search returns nothing for
# the literal bundle string — we have to extract a searchable prefix.
_OR_BUNDLE_RE = re.compile(r"\s+or\s+", re.IGNORECASE)
# Match "120g-142g" / "2.6kg-2.8kg" — keep the first weight only.
_WEIGHT_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:g|kg|ml|l|each|pk|pack))\s*-\s*\d+(?:\.\d+)?\s*(?:g|kg|ml|l|each|pk|pack)",
    re.IGNORECASE,
)


def _searchable_query(name: str) -> str:
    """Reduce a StockUp catalogue name to something Coles' search can find.

    Two-step cleanup:
      1. " or " bundles  → keep only the prefix before the first " or "
      2. weight ranges   → "120g-142g" becomes "120g"

    Returns the original name if neither pattern matches, so this is
    safe to call on every query (no regressions on clean names like
    "Tim Tam Original 200g").
    """
    parts = _OR_BUNDLE_RE.split(name, maxsplit=1)
    if len(parts) > 1:
        name = parts[0].rstrip()
    name = _WEIGHT_RANGE_RE.sub(r"\1", name)
    return name.strip()


@asynccontextmanager
async def build_browser_context() -> AsyncIterator[BrowserContext]:
    """Start Playwright + Chromium with stealthy defaults.

    Use as `async with build_browser_context() as ctx: ...`. The browser
    + context are cleanly closed on exit even if the caller raises.

    Persistent cookies are kept in-memory only — fine for a one-shot
    backfill; a longer-lived script could pass a `storage_state` path.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-AU",
                # Mask the "I am Playwright" navigator.webdriver flag.
                java_script_enabled=True,
            )
            # Extra stealth: blank out navigator.webdriver before any page
            # script runs.
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            try:
                yield context
            finally:
                await context.close()
        finally:
            await browser.close()


async def lookup_coles_image(
    context: BrowserContext,
    product_name: str,
    log: logging.Logger,
) -> ImageLookupResult:
    """Look up the image URL for one Coles product.

    Never raises. Misses return ImageLookupResult(image_url=None, error=...).
    """
    if not product_name.strip():
        return ImageLookupResult(None, None, "miss", error="empty product name")

    # Normalise once. Searches + scoring both use the cleaned name so a
    # StockUp bundle ("X or Y 100g-200g") becomes a real product query.
    search_name = _searchable_query(product_name)
    if not search_name:
        return ImageLookupResult(None, None, "miss", error="empty after normalise")

    page: Page | None = None
    try:
        page = await context.new_page()
        # Block heavy assets to keep page weight down.
        await page.route(
            "**/*.{woff,woff2,ttf,mp4,webm,gif}",
            lambda route: asyncio.create_task(route.abort()),
        )

        url = _COLES_SEARCH.format(q=_url_q(search_name))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_PAGE_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            return ImageLookupResult(None, None, "miss", error="coles_page_load_timeout")

        # Detect Cloudflare interstitial — page body briefly contains
        # "Just a moment" or "cf-browser-verification".
        body_text = (await page.content())[:4000].lower()
        if "just a moment" in body_text or "cf-browser-verification" in body_text:
            return ImageLookupResult(None, None, "miss", error="coles_cloudflare_challenge")

        # Wait for product tiles to render. Selector covers the current
        # Coles product card; we fall back to any /product/ link if that
        # specific data-testid drifts.
        try:
            await page.wait_for_selector(
                'a[href^="/product/"]', timeout=_PRODUCT_TILE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return ImageLookupResult(None, None, "miss", error="coles_no_tiles_rendered")

        # Pull candidate cards out of the rendered DOM.
        cards = await page.evaluate(
            """
            () => {
              const anchors = Array.from(document.querySelectorAll('a[href^="/product/"]'));
              const seen = new Set();
              const out = [];
              for (const a of anchors) {
                const href = a.getAttribute('href');
                if (!href || seen.has(href)) continue;
                seen.add(href);
                const aria = a.getAttribute('aria-label') || '';
                const inner = a.innerText || '';
                const img = a.querySelector('img');
                const imgSrc = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
                const imgAlt = img ? (img.getAttribute('alt') || '') : '';
                out.push({ href, aria, inner, imgSrc, imgAlt });
                if (out.length >= 30) break;
              }
              return out;
            }
            """
        )

        # Score candidates against the normalised name so bundle artifacts
        # ("or Conditioner 600mL") don't poison the overlap denominator.
        best = _pick_best_card(search_name, cards)
        if best is None:
            return ImageLookupResult(None, None, "miss", error="coles_no_card_match")

        image_url, product_href, score = best
        product_url = _absolute_url(product_href, _COLES_ORIGIN)

        # Card image first — it's usually a thumbnail with usable resolution.
        if image_url:
            return ImageLookupResult(
                image_url=_absolute_url(image_url, _COLES_ORIGIN),
                canonical_product_url=product_url,
                method="coles_search_card",
                score=score,
            )

        # Fallback: follow the product link + read og:image.
        og_url = await _fetch_og_image(context, product_url, log)
        if og_url:
            return ImageLookupResult(
                image_url=og_url,
                canonical_product_url=product_url,
                method="coles_product_og",
                score=score,
            )

        return ImageLookupResult(
            None, product_url, "miss", error="coles_no_image_in_card_or_og",
        )

    except Exception as e:  # noqa: BLE001
        log.exception(
            "coles.lookup_unexpected", extra={"name": product_name, "err": str(e)}
        )
        return ImageLookupResult(
            None, None, "miss", error=f"coles_unexpected: {type(e).__name__}: {e}",
        )
    finally:
        if page is not None:
            await page.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_og_image(
    context: BrowserContext, product_url: str, log: logging.Logger,
) -> str | None:
    page = await context.new_page()
    try:
        try:
            await page.goto(
                product_url,
                wait_until="domcontentloaded",
                timeout=_PAGE_LOAD_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            return None
        og = await page.evaluate(
            """
            () => {
              const og = document.querySelector('meta[property="og:image"]');
              if (og && og.getAttribute('content')) return og.getAttribute('content');
              const tw = document.querySelector('meta[name="twitter:image"]');
              if (tw && tw.getAttribute('content')) return tw.getAttribute('content');
              return null;
            }
            """
        )
        return og or None
    finally:
        await page.close()


def _pick_best_card(query: str, cards: list[dict]) -> tuple[str | None, str, float] | None:
    """Return (image_url, product_href, score) for the best-matching card."""
    q_tokens = _tokenise(query)
    if not q_tokens:
        return None

    best: tuple[str | None, str, float] | None = None
    for c in cards:
        candidate_name = (c.get("aria") or c.get("inner") or c.get("imgAlt") or "").strip()
        if not candidate_name:
            continue
        score = _token_overlap(q_tokens, _tokenise(candidate_name))
        if score < _MIN_TOKEN_OVERLAP:
            continue
        img_url = (c.get("imgSrc") or "").strip() or None
        href = c.get("href") or ""
        if not href:
            continue
        if best is None or score > best[2]:
            best = (img_url, href, score)
    return best


def _tokenise(s: str) -> set[str]:
    s = s.lower()
    s = _NORMALISE_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return {tok for tok in s.split(" ") if len(tok) > 1}


def _token_overlap(q: set[str], candidate: set[str]) -> float:
    if not q:
        return 0.0
    return len(q & candidate) / len(q)


def _absolute_url(href: str, prefix: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return f"{prefix}{href}"
    return f"{prefix}/{href}"


def _url_q(query: str) -> str:
    # Coles' search prefers spaces as %20, but + works too. Use urlencode-safe.
    import urllib.parse
    return urllib.parse.quote(query)


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from src.scrapers.base import configure_logging

    if len(sys.argv) < 2:
        print('Usage: python -m src.scrapers.coles_playwright "<product name>"',
              file=sys.stderr)
        sys.exit(2)

    log = configure_logging(verbose=True)
    name = " ".join(sys.argv[1:])

    async def main():
        async with build_browser_context() as ctx:
            result = await lookup_coles_image(ctx, name, log)
            print()
            print(f"query          = {name!r}")
            print(f"image_url      = {result.image_url}")
            print(f"product_url    = {result.canonical_product_url}")
            print(f"method         = {result.method}")
            print(f"score          = {result.score:.2f}")
            print(f"error          = {result.error}")

    asyncio.run(main())
