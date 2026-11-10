# Runs one county's probate scraper on this PC and pushes the results to GitHub.
# Used for counties whose court site blocks GitHub's servers (Summit).
#   powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -County summit
#   powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1 -County summit -DaysBack 120   # backfill
param([string]$County = "summit", [int]$DaysBack = 10, [int]$MaxMinutes = 120)

$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
$log = Join-Path (Get-Location) "data\$County\local_run.log"
New-Item -ItemType Directory -Force -Path "data\$County" | Out-Null
Start-Transcript -Path $log -Force | Out-Null

git stash --include-untracked | Out-Null
git pull --rebase
git stash pop | Out-Null
python run_pipeline.py --county $County --days-back $DaysBack --max-minutes $MaxMinutes
git add "data/$County" "docs/data/$County"
git commit -m "$County probate leads (local run) $(Get-Date -Format yyyy-MM-dd)"
for ($i = 0; $i -lt 3; $i++) { git pull --rebase; git push; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 10 }

Stop-Transcript | Out-Null
