# Both together — collector + web in one process
$py = . (Join-Path $PSScriptRoot "_env.ps1") -Install
Write-Host "Starting collector + web..." -ForegroundColor Cyan
Write-Host "  Open http://127.0.0.1:8001" -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop" -ForegroundColor DarkGray
& $py -m pmanalysis start
