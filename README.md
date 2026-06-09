# Polymarket Analysis

Uses **system Python** — no venv required.

## First time only

```powershell
cd D:\05working\polymarket-bot\completed\Polymarket-analysis-crack
pip install -e .
```

Or:

```powershell
.\scripts\setup.ps1
```

## Run backend + web separately

**Terminal 1 — backend:**
```powershell
.\scripts\collector.ps1
```

**Terminal 2 — web:**
```powershell
.\scripts\web.ps1
```

Open **http://127.0.0.1:8001**

## Run both together

```powershell
.\scripts\run.ps1
```

## Or without scripts

```powershell
python -m pmanalysis collector   # terminal 1
python -m pmanalysis web         # terminal 2
python -m pmanalysis start       # both in one
```

## Config

```powershell
$env:PM_PORT = "8000"
.\scripts\web.ps1
```
