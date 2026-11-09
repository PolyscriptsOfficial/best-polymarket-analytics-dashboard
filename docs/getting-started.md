# Getting Started

This guide covers installing, running, configuring and maintaining the Polymarket Analysis Tool, locally or on a VPS.

- [Requirements](#requirements)
- [Install](#install)
- [Run](#run)
- [Deploy on a VPS](#deploy-on-a-vps)
- [Configuration](#configuration)
- [Alerts](#alerts)
- [Data and storage](#data-and-storage)
- [Maintenance](#maintenance)
- [Backtesting](#backtesting)
- [Troubleshooting](#troubleshooting)

---

## Requirements

| Requirement | Notes |
|---|---|
| Python **3.11+** | System Python is fine; a virtual environment is optional |
| Network access | Polymarket (Gamma, CLOB), Chainlink via Polymarket RTDS, Binance |
| Disk | ~15 GB per year of Parquet archives; the SQLite hot store stays small |
| PM2 *(optional)* | For running as a managed service on a VPS |

## Install

```bash
git clone https://github.com/SuperAdam47/best-polymarket-analysis-tool.git
cd best-polymarket-analysis-tool
pip install -e .
```

On Windows you can use the helper instead, which installs into system Python:

```powershell
.\scripts\setup.ps1
```

## Run

### Single process (recommended)

Runs the collector and the web dashboard together.

```bash
python -m pmanalysis start
```

Windows helper (installs dependencies first, sets the default port):

```powershell
.\scripts\run.ps1
```

Then open **http://127.0.0.1:8420**.

### Separate processes

Run the collector and dashboard independently, for example to restart the UI without interrupting data collection. Both share the same SQLite database.

```bash
python -m pmanalysis collector   # terminal 1
python -m pmanalysis web         # terminal 2
```

Windows: `.\scripts\collector.ps1` and `.\scripts\web.ps1`.

### Pages

| Path | Purpose |
|---|---|
| `/` | Live terminal: price, TWAP, probability and candle charts per market |
| `/grid` | All six markets (BTC/ETH/SOL × 5m/15m) on one screen |
| `/history` | Resolved windows, outcomes, CSV export and dataset download |
| `/research` | Calibration, regime statistics and rule backtester |
| `/status` | Collector health, feed lag, gaps, database size, outcome audit |

## Deploy on a VPS

### Linux with PM2

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
pm2 start ecosystem.config.js
pm2 save
```

`ecosystem.config.js` runs `python -m pmanalysis start` as the app `pmanalysis`, with throttles tuned for small servers, automatic restart on crash and a 700 MB memory ceiling.

> Do **not** run `pm2 start pmanalysis` — PM2 would treat the package folder as a Node script and crash-loop with `MODULE_NOT_FOUND`.

Python logs go to stderr, so normal INFO lines appear in `pmanalysis-error.log`:

```bash
pm2 logs pmanalysis
```

### Windows Server

```powershell
.\scripts\run-vps.ps1
```

This binds to all interfaces, opens the Windows firewall for `PM_PORT`, prints every reachable URL and starts the collector and web server. If the dashboard is still unreachable, allow the port in your cloud provider's security group.

PM2 also works on Windows: `pm2 start ecosystem.config.js` uses `.venv` if present, otherwise system Python.

## Configuration

All settings are environment variables. Defaults suit a local machine; the PM2 config pins the small-VPS values.

**Server**

| Variable | Default | Description |
|---|---|---|
| `PM_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` for local-only access |
| `PM_PORT` | `8420` | HTTP port |

**Collector and live push**

| Variable | Default | Description |
|---|---|---|
| `PM_LIVE_PUSH_MS` | `150` | Live UI push interval |
| `PM_QUOTE_PUSH_SEC` | `0.25` | Quote push heartbeat |
| `PM_BOOK_POLL_SEC` | `3.0` | How often to check for stale order books |
| `PM_CLOB_STALE_SEC` | `8.0` | Age after which a book is refreshed over REST |
| `PM_ROLL_PREFETCH_SEC` | `30` | Seconds before a roll to subscribe to the next window |
| `PM_FEED_RECORD` | `0` | `1` records raw Binance / oracle / CLOB streams (debug only; grows SQLite quickly) |

**Storage**

| Variable | Default | Description |
|---|---|---|
| `PM_TICK_RETENTION_HOURS` | `24` | Hours of 1s ticks kept in SQLite after archiving |
| `PM_RETENTION_SWEEP_SEC` | `1800` | Interval between retention sweeps |

**Public dataset API (`/v1`)**

| Variable | Default | Description |
|---|---|---|
| `PM_DATASET_LOOKBACK_DAYS` | `30` | How far back `/v1` and downloads may reach |
| `PM_DATASET_TTL_DAYS` | `7` | Cache lifetime of built dataset zips |
| `PM_OWNER_CONTACT` | `https://polyscripts.world` | Contact shown when a request is outside the window |

**Model**

| Variable | Default | Description |
|---|---|---|
| `PM_FAIR_VOL_LOOKBACK_SEC` | `900` | Chainlink volatility lookback for fair value |
| `PM_LEAD_LAG_LOOKBACK_SEC` | `600` | Lookback for the Binance lead estimate |

Full list with comments: [`pmanalysis/config.py`](../pmanalysis/config.py).

Example (PowerShell):

```powershell
$env:PM_HOST = "127.0.0.1"
$env:PM_PORT = "9000"
python -m pmanalysis start
```

Example (bash):

```bash
PM_HOST=127.0.0.1 PM_PORT=9000 python -m pmanalysis start
```

## Alerts

Edge, YES+NO arbitrage and feed-gap alerts are shown in the browser. To also send them to Discord or Telegram:

| Variable | Default | Description |
|---|---|---|
| `PM_ALERT_DISCORD_WEBHOOK` | — | Discord webhook URL |
| `PM_ALERT_TELEGRAM_TOKEN` | — | Telegram bot token |
| `PM_ALERT_TELEGRAM_CHAT_ID` | — | Telegram chat id |
| `PM_ALERT_EDGE_CENTS` | `10` | Minimum model edge to alert |
| `PM_ALERT_ARB_CENTS` | `1` | Minimum YES+NO arbitrage gap |
| `PM_ALERT_MIN_SECS_LEFT` | `5` | Skip alerts this close to the window end |
| `PM_ALERT_COOLDOWN_SEC` | `300` | Minimum seconds before the same alert repeats |

Alerts are skipped while the Chainlink spot or a CLOB quote is stale.

## Data and storage

Everything lives under `data/`:

| Path | Contents |
|---|---|
| `data/pmanalysis.db` | SQLite hot store: window catalog, recent 1s ticks, research snapshots |
| `data/archive/*.parquet` | Full 1s history of every closed window (zstd) |
| `data/exports/` | CSV exports and built dataset zips |

Windows are archived to Parquet when they close. SQLite ticks older than `PM_TICK_RETENTION_HOURS` are then removed; History and Research read the Parquet archive for older windows. Schema migrations run automatically on start.

## Maintenance

```bash
# Shrink SQLite: drop raw feeds and archived ticks past retention (keeps windows + Parquet)
python -m pmanalysis prune-db -y --vacuum

# Remove closed windows with incomplete ticks or missing outcomes
python -m pmanalysis purge-broken-windows --dry-run
python -m pmanalysis purge-broken-windows -y [--strict]

# Build research snapshots for windows recorded before the Research page existed
python -m pmanalysis backfill-features [--limit N]
```

Add `-v` before the command for verbose logging, for example `python -m pmanalysis -v start`.

## Backtesting

**Local stored data**

```powershell
.\scripts\backtest.ps1           # YES/NO ask reversal counts
.\scripts\backtest_btc_grid.ps1  # BTC parameter grid scan
```

The `/research` page has a rule backtester with fees, slippage and size checks.

**Remote dataset**

Another machine can download one market and date range from a running collector — no token required:

```bash
python scripts/fetch_dataset.py --url http://<collector>:8420 \
  --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z \
  --asset BTC --interval 5m
```

Options: `--timeout` (default 3600 s, or `PM_DATASET_TIMEOUT`) and `--poll` (default 1 s). The dataset contract is documented in [history-dataset.md](history-dataset.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard unreachable from another machine | Check `PM_HOST` is `0.0.0.0`, open the port in the OS firewall and cloud security group |
| PM2 crash-loops with `MODULE_NOT_FOUND` | Start with `pm2 start ecosystem.config.js`, not `pm2 start pmanalysis` |
| CLOB "slow consumer" disconnects or high CPU | Use the PM2 throttle values (`PM_QUOTE_PUSH_SEC=0.25`, `PM_BOOK_POLL_SEC=3`, `PM_LIVE_PUSH_MS=150`) |
| SQLite file keeps growing | Make sure `PM_FEED_RECORD=0`, then run `prune-db -y --vacuum` |
| Research page empty for older windows | Run `python -m pmanalysis backfill-features` |
| `/v1` request rejected | It is outside the `PM_DATASET_LOOKBACK_DAYS` window |
