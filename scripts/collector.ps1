# Backend only — Polymarket data collector (24/7 feeds + SQLite)
$py = . (Join-Path $PSScriptRoot "_env.ps1") -Install
if (-not $env:PM_FEED_RECORD) { $env:PM_FEED_RECORD = "0" }
Write-Host "Starting collector (backend)..." -ForegroundColor Cyan
Write-Host "  Stores data to data\pmanalysis.db" -ForegroundColor DarkGray
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
& $py -m pmanalysis collector
