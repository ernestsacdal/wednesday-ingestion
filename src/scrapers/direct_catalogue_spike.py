"""Phase 0 spike: can we scrape Coles + Woolworths catalogues directly?

After the OzBargain spike (2026-05-12) revealed the weekly half-price post is
authored by competitor StockUpApp, the catalogue PDF / direct-scrape "plan C"
needs to graduate to a co-equal data source. This spike probes whether direct
access is even feasible before we commit Phase 1 work to it.

What we test for each retailer:
  1. Can we reach the specials page at all (no firewall, no immediate 403)?
  2. Is the response HTML or a Cloudflare challenge / captcha?
  3. Does the HTML contain price data or is everything JS-rendered (would need Playwright)?
  4. Is there an exposed JSON API we can hit (Woolies has one historically)?

Output is a verdict for each retailer: GREEN / YELLOW / RED with reasoning.

Run:
    python src/scrapers/direct_catalogue_spike.py
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

# Browser-ish user agent so we look like a person, not a bot.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"


@dataclass
class Probe:
    name: str
    url: str
    method: str = "GET"
    headers: dict[str, str] | None = None
    body: dict | None = None
    expect_json: bool = False


@dataclass
class Result:
    probe: Probe
    ok: bool
    status: int | None
    content_type: str
    size: int
    first_kb: str
    notes: list[str]


COLES_PROBES = [
    Probe(
        name="coles-specials-page",
        url="https://www.coles.com.au/on-special",
    ),
    Probe(
        name="coles-graphql-products",
        url="https://www.coles.com.au/api/bff/products",
        method="GET",
    ),
]

WOOLIES_PROBES = [
    Probe(
        name="woolies-specials-page",
        url="https://www.woolworths.com.au/shop/browse/specials",
    ),
    Probe(
        name="woolies-search-api",
        url="https://www.woolworths.com.au/apis/ui/Search/products",
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        body={
            "SearchTerm": "",
            "PageSize": 24,
            "PageNumber": 1,
            "Filters": [{"Key": "SoldBy", "Value": "Woolworths"}],
            "IsSpecial": True,
        },
        expect_json=True,
    ),
]


BOT_CHALLENGE_SIGNS = [
    "cf-challenge", "cf_chl", "Cloudflare", "Just a moment",
    "DataDome", "PerimeterX", "px-captcha", "Akamai",
    "verify you are human", "captcha",
]


def run_probe(probe: Probe) -> Result:
    print(f"\n  Probing: {probe.name}")
    print(f"    {probe.method} {probe.url}")

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"}
    if probe.headers:
        headers.update(probe.headers)

    notes: list[str] = []
    try:
        if probe.method == "GET":
            resp = requests.get(probe.url, headers=headers, timeout=20, allow_redirects=True)
        else:
            resp = requests.post(
                probe.url,
                headers=headers,
                json=probe.body,
                timeout=20,
                allow_redirects=True,
            )
    except requests.RequestException as e:
        return Result(
            probe=probe, ok=False, status=None, content_type="",
            size=0, first_kb="", notes=[f"Request failed: {e}"],
        )

    body = resp.text
    ct = resp.headers.get("content-type", "")
    first_kb = body[:1024]

    # Bot-challenge detection
    body_lower = body.lower()
    for sign in BOT_CHALLENGE_SIGNS:
        if sign.lower() in body_lower:
            notes.append(f"Bot-challenge signal detected: '{sign}'")
            break

    # Save raw for later inspection
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"{probe.name}.{('json' if probe.expect_json else 'html')}"
    out_path.write_text(body, encoding="utf-8")
    notes.append(f"Saved {len(body)} bytes to {out_path.name}")

    if probe.expect_json:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                product_count = len(data.get("Products") or data.get("products") or [])
                notes.append(f"JSON parsed; product-like keys count: {product_count}")
        except json.JSONDecodeError:
            notes.append("Expected JSON but body is not valid JSON")
    else:
        # Look for product-like markers in HTML
        price_count = len(re.findall(r"\$\d+(?:\.\d{2})?", body))
        product_links = len(re.findall(r"/product/|product-card|productTile", body))
        notes.append(f"$-prices found: {price_count}, product markers: {product_links}")

    ok = resp.status_code == 200 and not any("Bot-challenge" in n for n in notes)
    return Result(
        probe=probe, ok=ok, status=resp.status_code,
        content_type=ct, size=len(body), first_kb=first_kb, notes=notes,
    )


def verdict(results: list[Result]) -> tuple[str, str]:
    """Aggregate per-retailer verdict: GREEN / YELLOW / RED + reasoning."""
    any_blocked = any("Bot-challenge" in n for r in results for n in r.notes)
    any_200 = any(r.status == 200 for r in results)
    any_403 = any(r.status == 403 for r in results)
    any_json_ok = any(
        r.probe.expect_json and r.ok and "JSON parsed" in " ".join(r.notes)
        for r in results
    )
    has_prices = any(
        any("$-prices found: " in n and not n.endswith(": 0,") for n in r.notes)
        for r in results
    )

    if any_json_ok:
        return "GREEN", "JSON API responded with product data — easy structured scrape path"
    if any_blocked:
        return "RED", "Bot-challenge detected — would need Playwright + stealth, brittle"
    if any_403:
        return "RED", "403 Forbidden — direct scraping blocked"
    if not any_200:
        return "RED", "No probe returned 200 — endpoints may have moved"
    if has_prices:
        return "YELLOW", "HTML reachable with $-prices visible, but parsing JS-rendered SPA is fragile"
    return "YELLOW", "HTML reachable but no price markers visible — likely JS-rendered, needs Playwright"


def main() -> int:
    print("=" * 72)
    print("Wednesday — Phase 0 spike: direct Coles + Woolies catalogue access")
    print("=" * 72)

    print("\n--- Coles ---")
    coles_results = []
    for p in COLES_PROBES:
        r = run_probe(p)
        for n in r.notes:
            print(f"      - {n}")
        print(f"      status={r.status}  size={r.size}  ok={r.ok}")
        coles_results.append(r)
        time.sleep(2)  # polite delay

    print("\n--- Woolworths ---")
    woolies_results = []
    for p in WOOLIES_PROBES:
        r = run_probe(p)
        for n in r.notes:
            print(f"      - {n}")
        print(f"      status={r.status}  size={r.size}  ok={r.ok}")
        woolies_results.append(r)
        time.sleep(2)

    coles_verdict, coles_reason = verdict(coles_results)
    woolies_verdict, woolies_reason = verdict(woolies_results)

    print("\n" + "=" * 72)
    print("VERDICTS")
    print("=" * 72)
    print(f"  Coles:      {coles_verdict:<6} — {coles_reason}")
    print(f"  Woolies:    {woolies_verdict:<6} — {woolies_reason}")
    print()
    print("GREEN  = direct scrape viable, low effort")
    print("YELLOW = needs Playwright (heavier, but doable)")
    print("RED    = blocked; depend on StockUp post for now")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
