# Web UI only — reads from SQLite (run collector.ps1 in another terminal)
$py = . (Join-Path $PSScriptRoot "_env.ps1") -Install
if (-not $env:PM_PORT) { $env:PM_PORT = "8420" }
Write-Host "Starting web dashboard..." -ForegroundColor Cyan
Write-Host "  Open http://127.0.0.1:$($env:PM_PORT) (or http://<vps-ip>:$($env:PM_PORT))" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
& $py -m pmanalysis web
