# Residential pull of Woolworths LIVE half-price data into Supabase.
#
# Why this exists: the live Woolworths browse API is blocked from datacenter
# IPs, so the cloud cron (GitHub Actions) can only fall back to the less-
# complete hotprices dump (~82% precision / ~57% recall). Run from a home /
# residential IP, the live API works and gives the full ~100% set. Register
# this as a Windows Scheduled Task so it runs unattended (see README/setup).
#
# It writes through the same refresh_woolies_specials path the cron uses, so
# the result is identical to a manual `python -m src.refresh_woolies_specials`.

$ErrorActionPreference = 'Stop'

$repo = 'C:\Users\sacda\perso\wednesday-ingestion'
$py   = Join-Path $repo '.venv\Scripts\python.exe'

# Log outside the repo so it never gets committed.
$logDir = Join-Path $env:LOCALAPPDATA 'Wednesday'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('woolies-refresh-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')

Set-Location $repo
"[$(Get-Date -Format o)] starting woolies live refresh" | Tee-Object -FilePath $log
& $py -X utf8 -m src.refresh_woolies_specials --verbose *>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
"[$(Get-Date -Format o)] finished exit=$code" | Tee-Object -FilePath $log -Append
exit $code
