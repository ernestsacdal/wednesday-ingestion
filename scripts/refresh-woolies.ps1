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

# Python's logging writes to STDERR. Under a hidden Scheduled Task,
# $ErrorActionPreference='Stop' turns that stderr into a terminating error, so
# the run dies right after the first log line (exit 1). Keep 'Continue' and
# stop PowerShell from treating native stderr / exit codes as terminating; we
# read the real exit code from $LASTEXITCODE ourselves.
$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false

$repo = 'C:\Users\sacda\perso\wednesday-ingestion'
$py   = Join-Path $repo '.venv\Scripts\python.exe'

# Log outside the repo so it never gets committed.
$logDir = Join-Path $env:LOCALAPPDATA 'Wednesday'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('woolies-refresh-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')

Set-Location $repo
# There's no console under a scheduled task, so write straight to the log file
# (Tee-Object piping native stderr is what broke the scheduled run).
"[$(Get-Date -Format o)] starting woolies live refresh" | Out-File -FilePath $log -Encoding utf8
& $py -X utf8 -m src.refresh_woolies_specials --verbose *>&1 | Out-File -FilePath $log -Append -Encoding utf8
$code = $LASTEXITCODE
"[$(Get-Date -Format o)] finished exit=$code" | Out-File -FilePath $log -Append -Encoding utf8
exit $code
