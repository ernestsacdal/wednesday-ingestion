# wednesday-ingestion

Public Python repo for Wednesday's data pipeline.

Runs weekly in GitHub Actions to scrape OzBargain's half-price reports, dedup products across retailers, generate cycle predictions, and write everything to Supabase.

Public so GitHub Actions cron runs on free unlimited minutes.

## Structure

```
src/
├── models.py                          # Shared dataclasses (WeeklySpecial, ScrapeRun, ScrapeOutput)
├── pipeline.py                        # CLI entry point
└── scrapers/
    ├── base.py                        # Polite HTTP session + structured logging
    ├── ozbargain_stockup.py           # Weekly Catalogue Half-Price Report (Coles + Woolies in one post)
    └── direct_catalogue_spike.py      # Phase 0 reference — direct Coles/Woolies probe (YELLOW verdict)

.github/workflows/
└── weekly-ingestion.yml               # Cron 16:00 UTC Tuesday ≈ 2am Wed Sydney

data/runs/                             # Local JSON dumps from pipeline runs (gitignored)
data/raw/                              # Raw HTML kept by spikes for debugging (gitignored)
```

## Running the pipeline

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Scrape + log to console, no writes:
python -m src.pipeline

# Scrape + write JSON dump to data/runs/<timestamp>.json:
python -m src.pipeline --write-json data/runs

# Verbose debug logging:
python -m src.pipeline --write-json data/runs --verbose
```

Exit codes: 0 = success, 1 = no_data (post not found / empty), 2 = failure.

## Running the predictor

```powershell
# Compute predictions from the latest scrape (glob supported, picks latest by name):
python -m src.prediction.statistical "data/runs/stockup_post_*.json" --output data/predictions

# Tighter gating (only emit when ≥3 historical cycles observed):
python -m src.prediction.statistical data/runs/stockup_post_20260512T094847Z.json --min-cycles 3
```

Output: a JSON file per run with a `summary` block (inputs considered, predictions emitted, gated out) and a `predictions` array. Each prediction has retailer, product name, predicted window, confidence (0-1 + tier), cycle count, mean/stddev intervals, and a plain-English rationale ready for the mobile prediction card.

**Confidence math**: `0.5 × cycle_score + 0.5 × dispersion_score` where `cycle_score = min(n/8, 1)` and `dispersion_score = 1 - min(stddev/mean, 1)`. Tier thresholds: ≥0.75 high, ≥0.45 medium, otherwise low. With single-cycle data most predictions land in low/medium; once historical backfill (Phase 1b) provides ≥8 cycles the high tier opens up.

## Phase 0 status

- [x] OzBargain weekly post is parseable — see [`docs/phase-0-findings.md`](../wednesday/docs/phase-0-findings.md) in the sibling repo.
- [x] Spike validated 181 products from latest catalogue (103 Coles + 78 Woolies), with built-in cycle data on 140/181.
- [x] Production scraper module split + JSON output schema match the eventual Supabase tables.
- [x] GitHub Actions weekly cron scaffolded (artifact-only until Supabase project exists).
- [x] Statistical cycle predictor (`src/prediction/statistical.py`) — 138 predictions from 178 products on real data.
- [x] Direct catalogue PDF discovery spike (`src/scrapers/catalogue_pdf_spike.py`) — RED verdict for both retailers, Playwright deferred to v1.1+.
- [ ] Write to Supabase once project + secrets are set up (DB-writing code path stubbed in `pipeline.py`).
- [ ] Historical backfill from older OzBargain posts (Phase 1b).
- [ ] Product matching/dedup + variety bundling (Phase 1b, requires DB).
- [ ] Predictor writes to Supabase `predictions` table (Phase 1b).

## Source attribution

Wednesday consumes the public weekly "Catalogue Half-Price Report" posts authored by user **`StockUpApp`** on the OzBargain public forum. We're grateful for their work compiling each week's catalogue — Wednesday's prediction features build on top of the cycle data their posts already include.

## Why is this repo public?

GitHub Actions cron is free + unlimited for public repos. This repo intentionally contains no secrets; Supabase service keys live in GitHub Actions secrets, never committed.
