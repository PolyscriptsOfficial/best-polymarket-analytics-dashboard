# Polymarket Analysis Tool

Live dashboard, tick storage, and backtesting for **Polymarket** BTC / ETH / SOL **Up or Down** markets (5m and 15m).

Streams Polymarket CLOB quotes, Chainlink TWAP, and Binance trades. Archives every window so you can replay history and run backtests.

**Python 3.11+** · system Python (no venv required)

---

## Features

Full write-up: **[FEATURES.md](FEATURES.md)** · Recent changes: **[CHANGELOG.md](CHANGELOG.md)**

**Live terminal** (`/`)
- BTC, ETH, SOL Up/Down — 5m and 15m
- Clock-based window rolls (charts + TWAP target reset at the 5m/15m boundary; countdown stays live)
- Chainlink spot + 60s TWAP, initial TWAP (price to beat) synced to chart target line, Binance overlay
- Reliable history fill when switching 5m/15m or after a roll (REST + WebSocket backfill)
- Polymarket CLOB YES/NO asks
- UP/DOWN candlesticks: 5m per candle on 5m markets, 15m per candle on 15m
- Recent-markets sidebar (current + past windows; not the blank next market)
- Load any slug or jump back to live; Open market on Polymarket

**History and status**
- `/history` — resolved windows, outcomes, replay charts, UP/DOWN win rate
- `/status` — collector health, feed lag, gaps, database size
- CSV export per window; parquet archive after each window closes

**Collector**
- Discovers current + next Gamma slugs; CLOB overlap ~30s before the roll
- 1-second merged ticks (oracle + CLOB + indicators)
- SQLite hot store (~24h of 1s ticks); parquet for older History charts
- Optional raw streams (`PM_FEED_RECORD=1`) for short debug captures

**Backtest**
- Replay stored ticks (no live API needed)
- YES/NO ask-reversal counts (e.g. 90¢ flips)
- BTC grid scan and zip export for offline research
- Remote period zip (`/v1/datasets`) so another PC can backtest one market + date range — [docs/history-dataset.md](docs/history-dataset.md)

---

## Quick start

```powershell
pip install -e .
.\scripts\run.ps1
```

Open **http://127.0.0.1:8000**

```powershell
# split processes
.\scripts\collector.ps1    # terminal 1
.\scripts\web.ps1          # terminal 2

# or without scripts
python -m pmanalysis start
python -m pmanalysis collector
python -m pmanalysis web

# VPS (Windows firewall + all interfaces)
.\scripts\run-vps.ps1
```

Linux: `pm2 start ecosystem.config.js`

---

## Data

| Table | Source |
|-------|--------|
| Store | What |
|-------|------|
| `ticks` (SQLite, ~24h) | 1s merged samples for the live UI |
| `windows` (SQLite) | Market catalog: slug, strike, outcome |
| `data/archive/*.parquet` | Full 1s history after each window closes |
| `binance_trades` / `oracle_ticks` / `clob_quotes` | Optional raw tape (`PM_FEED_RECORD=1` only) |

Raw feed recording is **off** by default. History charts load parquet when SQLite ticks have been pruned.

```powershell
$env:PM_FEED_RECORD = "1"   # optional debug capture
.\scripts\run.ps1

python -m pmanalysis prune-db -y --vacuum   # shrink live SQLite; keep parquet
```

Windows archive to `data/archive/*.parquet`. SQLite 1s ticks older than 24 hours are pruned.

---

## Backtest

```powershell
.\scripts\backtest.ps1
.\scripts\backtest.ps1 -All
.\scripts\backtest_btc_grid.ps1
```

Remote dataset (`/v1` is public; no token):

```powershell
python scripts/fetch_dataset.py --url http://<collector>:8000 `
  --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m
```

Contract for the backtest app: **[docs/history-dataset.md](docs/history-dataset.md)**

---

## Config

```powershell
$env:PM_PORT = "8000"
$env:PM_HOST = "127.0.0.1"          # default is 0.0.0.0
$env:PM_FEED_RECORD = "0"           # default; set 1 only for raw History tabs
$env:PM_TICK_RETENTION_HOURS = "24"
.\scripts\web.ps1
```

```text
python -m pmanalysis prune-db -y --vacuum  # keep windows + parquet; drop raw + old ticks
```

Research tool only — no trading, no financial advice.
