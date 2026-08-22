# Polymarket Analysis

Uses **system Python** - no venv required.

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

**Terminal 1 - backend:**
```powershell
.\scripts\collector.ps1
```

**Terminal 2 - web:**
```powershell
.\scripts\web.ps1
```

Open **http://127.0.0.1:8000**

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

## Feed stream recording

The collector also saves **every** Binance trade, Chainlink/oracle tick, and CLOB orderbook BBO change (batched to SQLite):

| Table | Source |
|-------|--------|
| `binance_trades` | Binance `@trade` WebSocket |
| `oracle_ticks` | Polymarket RTDS Chainlink (+ fallbacks) |
| `clob_quotes` | Polymarket CLOB market WebSocket |

Still kept: 1-second merged samples in `ticks`.

```powershell
# default ON - disable if needed:
$env:PM_FEED_RECORD = "0"
.\scripts\run.ps1
```
