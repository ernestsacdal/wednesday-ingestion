# wednesday-ingestion

[![daily-ingestion](https://github.com/ernestsacdal/wednesday-ingestion/actions/workflows/daily-ingestion.yml/badge.svg)](https://github.com/ernestsacdal/wednesday-ingestion/actions/workflows/daily-ingestion.yml)
[![weekly-catalogue](https://github.com/ernestsacdal/wednesday-ingestion/actions/workflows/weekly-catalogue.yml/badge.svg)](https://github.com/ernestsacdal/wednesday-ingestion/actions/workflows/weekly-catalogue.yml)

The data pipeline behind **Wednesday** — a free iOS app that predicts when
Australian groceries next go half-price at Coles and Woolworths. This repo
ingests both retailers' prices daily, derives every half-price event from
multi-year price history, runs a statistical cycle predictor with a
published track record, links the same product across both stores, sends
weekly watchlist push digests, and verifies its own output before calling
any run a success.

Pure Python 3.13, no framework, no server: everything runs as scheduled
GitHub Actions jobs writing to a Supabase Postgres that the app reads.

## How the data flows

```
hotprices.org daily dumps          Woolworths browse API
(Coles + Woolies, CDN-hosted)      (live "Half Price" category)
        |                                  |
        |          (fallback when the live |
        |           API is blocked in CI)  |
        v                                  v
  +---------------------------------------------+
  |       parse / derive (this repo, daily)     |
  |  half-price detection - categories -        |
  |  marketplace exclusion - real-SKU keying    |
  +---------------------------------------------+
        |                  weekly: predictor,
        v                  backtest, matcher
  Supabase Postgres  <---------------+
  (products, specials, observations,
   predictions, accuracy, aliases)
        |
        v
  verify_data.py  -- 15 invariants; any failure fails the
        |            workflow -> GitHub failure email
        v
  the Wednesday iOS app (private repo) reads the tables
```

The app never calls anything in this repo; it only reads finished tables.

## Data sources

| Source | Role | Cadence |
|---|---|---|
| [hotprices.org](https://hotprices.org) daily dumps ([Javex/hotprices-au](https://github.com/Javex/hotprices-au), MIT) | Coles prices + price history; Woolies fallback | Daily, ~01:00 UTC |
| Woolworths browse API | Woolies current-week half-price specials | Daily |

We deliberately do not scrape the retailers' websites ourselves. The
hotprices-au project already runs the browser and publishes a hosted JSON
dump every day; we consume that file and cache everything into our own
tables (`source='hotprices'`), so Wednesday owns its history even if the
upstream ever disappears.

The Woolworths live API is frequently blocked from GitHub Actions
datacenter IPs. When it returns nothing (or raises), the refresh falls
back to the hotprices Woolies dump automatically — losing a retailer
silently is not an acceptable failure mode here.

## What gets derived

The dumps carry no "was price" or "1/2 price" flag, so everything is
derived from each product's price-history change points:

- **Half-price events** — a change point is a half-price event when the
  new price is at least 48% below the price it dropped from. A product is
  *currently* half-price when its newest change point is an event, or its
  current price sits >=48% below its recent regular price. Items 30-47%
  off are stored as ordinary specials (`is_half_price=false`) and never
  feed the predictor.
- **Regular price** — the maximum price observed in the last 300 days
  (falls back to all history).
- **Real-SKU keying** — products are keyed `coles:<id>` /
  `woolworths:<stockcode>` using the retailers' real ids, so two distinct
  products that share a name never collapse into one row, and product
  image URLs become deterministic CDN lookups (100% image coverage, no
  image scraping).
- **Categories** — the dumps' numeric category codes are mapped through a
  vendored copy of the hotprices-au taxonomy (144 codes), with a narrow,
  ordered keyword fallback for the ~30% of items that carry no code.
  Anything that matches nothing stays honestly `Uncategorised`.
- **Marketplace exclusion** — roughly 73% of the Woolies dump is
  third-party "Everyday Market" stock (general merchandise with inflated
  was-prices), not supermarket groceries. Real Woolworths stockcodes are
  <=7 digits; marketplace ids are 10 — a perfectly bimodal split — so
  those items (plus a small non-grocery category denylist) are dropped at
  parse time. The honest catalogue is ~41.6k supermarket products
  (~21.3k Coles + ~20.3k Woolies), of which ~2,400 are half-price in a
  typical week.

## The predictor, and its measured track record

`src/prediction/statistical.py` models each product's half-price cycle
from its full sale history: mean interval between sales, dispersion, and
a predicted next window. Confidence is
`0.5 * cycle_score + 0.5 * dispersion_score`, with honesty gates layered
on top: products with fewer than 3 observed cycles are capped at low
confidence ("warming up"), window half-widths are floored at +/-1 week,
and confidence is capped at 0.95 — the model is never allowed to sound
more certain than its data.

The claim "this works" is measured, not asserted. `src/backtest.py`
replays every product's sale history through the production predictor
(walk-forward, first window after each sale, warming-up rules applied
as-of prediction time) and scores whether the next sale actually landed
in the predicted window:

| Confidence tier | Windows tested | Hit rate |
|---|---|---|
| High | ~10,700 | **73.5%** |
| Medium | ~18,400 | 68.8% |
| Low | ~14,800 | 49.6% |
| Overall | ~44,000 | 63.5% |

The tiers order correctly, and the numbers are published inside the app
(per-product last-6 tally plus the global rate), recomputed weekly.

## Cross-store matching

`src/match_counterparts.py` links the same product at Coles and
Woolworths so the app can show "also at the other store, cheaper here".
Precision comes first — a wrong link is worse than a missing one:

- quantities are parsed to canonical totals (`5x70g` == `350g`) and must
  agree when both names state one; pack counts likewise
- the leading brand token of one name must appear in the other
  (kills `Coles Storage Bags` vs `Glad Storage Bags`)
- variant tokens are guarded (strict on colours and heat — `Hot` never
  matches `Mild`; subset-tolerant on flavours)
- price ratio sanity caps, similarity thresholds, and mutual-best
  one-to-one assignment

Result: ~5,300 links. An adversarial audit of a 100-pair sample judged
87 same / 0 wrong / 3 unsure.

## The safety net

The design principle: **a green run must mean the data is right**, not
merely that the scripts didn't crash. Several early incidents ran green
in CI while a retailer's week was silently empty — that class of failure
is now engineered away:

- `src/verify_data.py` asserts 15 invariants against the live database at
  the end of every workflow — per-retailer current-week floors, week
  freshness, no marketplace or legacy rows, catalogue floors, both
  retailers scraped within 26h, predictions/accuracy/aliases fresh,
  price sanity, category coverage. Any violation exits non-zero, fails
  the workflow, and triggers GitHub's failure email.
- Writers take a Postgres advisory lock, so a manual run and the cron can
  never interleave.
- A partial scrape (API died mid-pagination) writes upserts only — an
  incomplete snapshot can never delete rows it didn't see.
- Suspiciously small dumps (<10k items vs the usual 21k/72k) are rejected
  outright rather than treated as authoritative.
- Destructive one-off scripts (`purge_*.py`) are dry-run by default and
  require `--confirm`.

## Schedules

| Workflow | Cron (UTC) | Steps |
|---|---|---|
| `daily-ingestion.yml` | 02:00 daily | Coles refresh (dump) -> Woolies refresh (API, dump fallback) -> image fill -> push digests (`send_alerts`, once per device per week via DB dedup) -> **verify_data** |
| `weekly-catalogue.yml` | 03:00 Sunday | full catalogue load -> predictor -> backtest (track record) -> cross-store matcher -> **verify_data** |

## Module map

```
src/
├── pipeline.py                  daily orchestrator (refreshes + images + alerts)
├── refresh_coles_hotprices.py   Coles current-week half-price from the dump
├── refresh_woolies_specials.py  Woolies current week: live API + dump fallback
├── ingest_catalogue.py          weekly full catalogue -> products (search-only)
├── backfill_history.py          one-time deep half-price history per retailer
├── verify_data.py               post-run invariants; non-zero exit fails the run
├── backtest.py                  walk-forward predictor backtest -> accuracy tables
├── match_counterparts.py        cross-store product matcher -> product_aliases
├── send_alerts.py               weekly watchlist push digests (Expo)
├── env.py                       shared .env loading
├── purge_marketplace.py         one-off cutover (dry-run first)
├── purge_synthetic_skus.py      one-off cutover (dry-run first)
├── db/
│   ├── writer.py                row-by-row writer + predictions writer
│   ├── bulk_writer.py           set-based writes + current-week sync (advisory-locked)
│   └── reader.py                loads sale history for the predictor
├── scrapers/
│   ├── base.py                  polite HTTP session + structured logging
│   ├── hotprices.py             dump fetch/parse, half-price derivation, categories,
│   │                            marketplace exclusion (both retailers)
│   ├── woolies_specials.py      Woolworths browse-API scraper
│   └── product_images.py        image discovery helpers
└── prediction/
    └── statistical.py           cycle predictor (pure stdlib statistics)

.github/workflows/
├── daily-ingestion.yml
└── weekly-catalogue.yml
```

## Running locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# .env in the repo root (use the Supabase Session Pooler URI - IPv4):
#   SUPABASE_DB_URL=postgresql://...

# Full daily pipeline (both retailers + images), writing to the DB:
python -m src.pipeline --write-db --verbose

# One-off refreshes (~10s each via the bulk writer):
python -m src.refresh_coles_hotprices --verbose
python -m src.refresh_woolies_specials --verbose
python -m src.refresh_woolies_specials --force-fallback --verbose   # exercise the dump fallback

# Derived data:
python -m src.prediction.statistical --from-db --write-db --min-cycles 1
python -m src.backtest --write-db
python -m src.match_counterparts --write-db

# Check the invariants any time:
python -m src.verify_data --verbose
```

Conventions: every CLI exits 0 on success and non-zero on failure (so CI
steps fail loudly); destructive scripts dry-run by default; all writes
are idempotent upserts, safe to re-run.

## Why this repo is public

GitHub Actions cron minutes are free and unlimited for public repos. The
repo contains no secrets — the database URL lives in GitHub Actions
secrets and a local `.env` (gitignored), never in code.

## Attribution

Wednesday's price data builds on the open-source
[hotprices-au](https://github.com/Javex/hotprices-au) project (MIT) by
Javex, which scrapes and publishes daily price dumps for Australian
supermarkets — they run the browser so nobody else has to. Early
prototypes also drew on StockUp's public weekly half-price reports on
OzBargain (retired as a source in June 2026).
