# Midday atomic promo-week roll (ADR-0001 in the wednesday repo).
#
# Why this exists: the app's "current week" is max(week_start), and on
# Wednesdays the week must arrive WHOLE — on 2026-07-15 the Woolies-only
# morning task rolled the new week alone and live App Store users saw
# "Coles: 0 items" for hours (the cloud cron, scheduled noon AEST, has been
# firing 2.5-4h late on GitHub's congested scheduler). By 12:30 AEST the
# hotprices Coles dump is fresh (refreshes ~11am), so this task rolls the
# week atomically from the residential IP: fresh Coles + LIVE Woolies +
# Coles catalogue corrections + the new week's dinners. The cloud cron
# remains the backstop for days this machine is off.
#
# Every step has its own guards (solo-roll guard, stale-dump gate, sticky-live
# guard, dinner revalidation), so re-runs and out-of-order timing are safe.

$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false

$repo = 'C:\Users\sacda\perso\wednesday-ingestion'
$py   = Join-Path $repo '.venv\Scripts\python.exe'

$logDir = Join-Path $env:LOCALAPPDATA 'Wednesday'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('midday-roll-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')

Set-Location $repo
"[$(Get-Date -Format o)] starting midday atomic roll" | Out-File -FilePath $log -Encoding utf8

$steps = @(
    @('coles',   @('-m', 'src.refresh_coles_hotprices', '--verbose')),
    @('woolies', @('-m', 'src.refresh_woolies_specials', '--verbose')),
    @('audit',   @('-m', 'src.audit_accuracy', '--write-db', '--correct', '--verbose')),
    @('dinners', @('-m', 'src.generate_recipes', '--seed', '--write-db', '--revalidate', '--verbose'))
)

$final = 0
foreach ($step in $steps) {
    $name = $step[0]
    "[$(Get-Date -Format o)] step=$name starting" | Out-File -FilePath $log -Append -Encoding utf8
    & $py -X utf8 @($step[1]) *>&1 | Out-File -FilePath $log -Append -Encoding utf8
    $code = $LASTEXITCODE
    "[$(Get-Date -Format o)] step=$name exit=$code" | Out-File -FilePath $log -Append -Encoding utf8
    if ($code -ne 0 -and $final -eq 0) { $final = $code }
}

"[$(Get-Date -Format o)] finished exit=$final" | Out-File -FilePath $log -Append -Encoding utf8
exit $final
