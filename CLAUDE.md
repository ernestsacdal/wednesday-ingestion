# wednesday-ingestion — Claude Code context

Python pipeline for Wednesday's data spine. Public sibling repo to the private `wednesday/` monorepo (kept public so weekly GitHub Actions cron runs on free unlimited minutes).

## What this repo does

- Weekly scrape of the StockUp Catalogue Half-Price Report on OzBargain (~180 products/week, Coles + Woolies combined, structured HTML tables with built-in cycle data).
- Statistical cycle predictor — turns `last_halfprice_weeks_ago` history into predicted next-half-price windows with honest confidence scores.
- (Future) Direct Coles/Woolies catalogue scraper as resilience fallback. Currently RED per Phase 0 / Phase 1a spikes — deferred to v1.1+.
- (Future Phase 1b) Writes to Supabase `products` / `price_observations` / `specials` / `predictions` / `scrape_runs` tables. Currently `--write-json` only.

## Stack

- Python 3.13 via uv (macOS dev machine; CI pins 3.13 too)
- Local venv at `.venv/` (recreate with `uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt`)
- Deps: `requests`, `beautifulsoup4`, `lxml`, `python-dateutil` — pure stdlib for stats (no numpy/pandas yet)
- GitHub Actions for the weekly cron (`.github/workflows/weekly-ingestion.yml`, fires 16:00 UTC Tuesday ≈ 2am Wed Sydney)

## Critical files

```
src/
├── models.py                              # WeeklySpecial, ScrapeRun, ScrapeOutput, Prediction, PredictionRunSummary
├── pipeline.py                            # CLI: --write-json / --write-db (stubbed) / --verbose
├── scrapers/
│   ├── base.py                            # build_session() with retry + polite_get() + configure_logging()
│   ├── ozbargain_stockup.py               # PRODUCTION scraper — find latest StockUp catalogue post, parse tables
│   ├── catalogue_pdf_spike.py             # Phase 1a spike — RED verdict (deferred, do not extend without re-evaluating)
│   └── direct_catalogue_spike.py          # Phase 0 spike — YELLOW verdict (kept as reference)
├── prediction/
│   └── statistical.py                     # Pure-stdlib cycle predictor — mean ± stddev, gated by min_cycles
└── (planned, Phase 1b)
    ├── db/supabase_client.py              # writes WeeklySpecial → products/observations/specials, ScrapeRun → scrape_runs
    ├── matching/dedup.py                  # cross-source product matching
    ├── matching/variety_bundling.py       # flavour/pack collapse → product_aliases
    └── prediction/prophet.py              # v2 predictor (post-launch)
```

## Run locally

```bash
# Setup (once)
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
# .env in the repo root: SUPABASE_DB_URL=<Session Pooler URI>, optional GROQ_API_KEY

# Tests (pure logic, no DB)
.venv/bin/python -m pytest

# Daily pipeline + invariants (see README "Running locally" for the full list)
.venv/bin/python -m src.pipeline --write-db --verbose
.venv/bin/python -m src.verify_data --verbose

# Residential launchd jobs (06:00 Woolies live refresh, 12:30 midday roll)
scripts/install-launchd.sh               # --uninstall to remove; logs in ~/Library/Logs/Wednesday/
```

Module imports use `from src.X import Y` — must run from the repo root so `src` is on `PYTHONPATH`. The CLI assumes this (calling `python -m src.pipeline` from anywhere else will fail with `ModuleNotFoundError: src`).

## Output schema

JSON dumps under `data/runs/` and `data/predictions/` are shaped to map 1:1 onto the eventual Supabase tables. See `src/models.py` for the canonical dataclasses. Prices are integer cents. Dates are ISO YYYY-MM-DD. Timestamps are tz-aware UTC.

## Important "don't" rules

1. **Don't add a fallback to scrape Coles or Woolies directly** without re-evaluating Phase 1a findings (`../wednesday/docs/phase-0-findings.md`). Both sites are heavy SPAs + Akamai-protected; direct scraping is a 1-2 week Playwright project deferred to v1.1+.
2. **Don't aggressively scrape OzBargain.** We hit the site at most twice per cron run (user profile page + the post itself) at 1 req/sec. Maintain that posture.
3. **Don't reach out to Samwise** — spec was wrong, the weekly post is by `StockUpApp` (a direct Wednesday competitor). Consume publicly, no contact.
4. **Don't introduce heavy ML deps** (numpy, pandas, Prophet) until Phase 1b/v1.1 when real backfill exists. Current statistical predictor is pure stdlib by design.
5. **Don't bypass the polite delay** in `polite_get()` — it's the only thing keeping us under OzBargain's threshold.

## Where this fits

This repo is one of two; the main project is at `../wednesday/` (private monorepo with mobile + web + supabase migrations, and the up-to-date `CLAUDE.md`). Build plan is at `~/.claude/plans/lets-plan-on-how-bright-fiddle.md` (carried over from the old Windows machine, if copied). Cross-repo Phase 0 findings doc is `../wednesday/docs/phase-0-findings.md`.

## Phase status

- Phase 0: complete
- Phase 1a: complete — scraper hardened, predictor shipped, GitHub Actions workflow scaffolded, catalogue PDF spike concluded RED
- Phase 1b (blocked): Supabase write path stubbed at `pipeline.py --write-db`, needs Supabase project + service key
- Phase 1b (blocked): 12-month historical backfill against older OzBargain catalogue posts
- Phase 1b (blocked): predictor writes to `predictions` table instead of JSON
