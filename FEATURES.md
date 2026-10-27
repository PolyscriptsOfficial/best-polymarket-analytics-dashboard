# PolyScripts — product features

PolyScripts is a live analysis terminal for **Polymarket crypto Up or Down** markets. It watches BTC, ETH, and SOL on **5-minute** and **15-minute** windows, records every window, and lets you replay history.

It does **not** place trades. It is a research / monitoring tool.

---

## Markets

| Asset | Intervals | Settlement idea |
|-------|-----------|-----------------|
| BTC, ETH, SOL | 5m and 15m | Up or Down vs the window’s **initial Chainlink TWAP** (60s) |

Six live markets at once: `BTC 5m`, `BTC 15m`, `ETH 5m`, `ETH 15m`, `SOL 5m`, `SOL 15m`.

Each window has a Polymarket slug such as `btc-updown-5m-1793082000`. Open Market links go to the matching Polymarket event page.

---

## Terminal (`/`)

The main screen is a trading-style layout: market list, live workspace, recent windows.

### Market nav

- One button per asset × interval
- Badge shows live YES probability
- Green / red tint follows whether price is above or below the target

### Live HUD

- **Initial TWAP** — Chainlink TWAP at window open (the price to beat)
- **Chainlink** — current oracle spot
- **TWAP 60s** — live Chainlink TWAP
- **Difference** — current TWAP − initial TWAP
- **Countdown** — time left in the clock bucket (keeps running across rolls)

Top ticker shows BTC / ETH / SOL spot plus the 5m TWAP difference.

### Price chart

Lightweight Charts of the **current window**:

- Binance spot
- Chainlink oracle
- 60s TWAP
- **TWAP Target** dashed line = same value as **Initial TWAP** in the HUD (window-open Chainlink TWAP, not live 60s TWAP)

Time axis is elapsed time inside that window (0:00 → 5:00 or 15:00).

### Live chart behavior

- Switching **asset** or **interval** (e.g. BTC 5m → BTC 15m) reloads the current clock window’s tick history so price and probability lines show the full elapsed window, not only the latest live point.
- After a **clock roll**, series reset, history is fetched again, and the TWAP target updates when the new window’s initial TWAP is known (from live market cards or stored ticks).
- Replaying an older slug from the dropdown or sidebar loads that window only; live mode ignores ticks whose `start_ts` is before the current clock bucket.

### Probability chart

YES ask and NO ask from the Polymarket CLOB, in cents (0–100).

### UP / DOWN candle chart

Sits **under** the YES/NO chart.

- 5m market → **one 5-minute candle per window**
- 15m market → **one 15-minute candle per window**
- Open = initial TWAP (price to beat)
- High / low / close = oracle range in that window
- **Green = UP**, **red = DOWN**
- The last candle updates live
- Click a candle to load that window’s tick charts

### Window picker

- Dropdown of recent slugs (live + resolved)
- Paste / type a slug and Load
- **Live** jumps back to the current clock window

### Recent markets (right sidebar)

Mini YES/NO path for the **current** window and previous resolved windows.

- LIVE badge on the window you are watching live
- UP / DOWN on finished windows
- Click a card to replay that slug
- The **next** (not-yet-started) market is **not** listed — it would be a blank card

---

## Window rolls

Windows follow the **clock**, not Gamma’s `active` flag.

1. At each 5m / 15m boundary the dashboard snaps to the new slug immediately.
2. Countdown resets to the new bucket; it does not freeze at `00:00`.
3. About **30 seconds** before the cut, the collector prefetches the next Gamma market and overlap-subscribes its CLOB tokens so the first ticks of the new window already have quotes.
4. If Gamma is late, a stub window is used until tokens arrive. The UI still rolls.
5. Stale ticks from the previous window are ignored.
6. **Charts and HUD** reset for the new bucket: price/probability series clear, tick history reloads, **Initial TWAP** and **TWAP Target** stay in sync when the open TWAP arrives, live candle starts a new bar, and nav badges / ticker / slug list refresh.
7. Until opening oracle and initial TWAP are known, the UI polls `/api/markets` every few seconds (no change to slug required).

You can keep watching live through the roll without picking a new slug.

---

## History (`/history`)

- Catalog of stored windows (slug, strike, outcome, tick count)
- UP win rate per asset × interval
- Replay charts for a selected window (same price + probability view)
- Optional raw tabs (Binance / Oracle / CLOB) only if feed-record was on
- CSV export per window

Older windows still chart after SQLite ticks are pruned — the UI reads **parquet archives**.

---

## Status (`/status`)

- Feed lag: Binance, Chainlink (RTDS), CLOB
- Collector running, last 1s tick, last discovery
- Database path and size
- Window / tick counts
- Open and recent **feed gaps** (stale or disconnected sockets)

---

## Data the collector stores

| Store | Role |
|-------|------|
| SQLite `windows` | Catalog: slug, times, strike, opening price, outcome |
| SQLite `ticks` | Hot 1-second samples for ~**24 hours** |
| SQLite `candles` | One OHLC bar per window (5m or 15m) |
| `data/archive/*.parquet` | Full 1s history after a window is archived |
| Raw Binance / Oracle / CLOB tables | **Off** by default (`PM_FEED_RECORD=0`) |

**Why both SQLite and parquet?** SQLite is the live hot store (small, fast). Parquet is the cold archive so History and candles of windows older than 24h still work without a huge database.

1-second ticks are the analysis grain: HUD, line charts, indicators, and backtests all share that series. Candles are a **window-sized** summary of those ticks, not a second feed.

---

## Live feeds

| Feed | What it is used for |
|------|---------------------|
| Polymarket Gamma | Discover current + next market slugs and tokens; resolution |
| Polymarket CLOB WebSocket | YES/NO best bid/ask (REST book only if the socket goes stale) |
| Polymarket RTDS | Chainlink spot + 60s TWAP |
| Binance spot | Overlay on the price chart |

Merged **1s ticks** also compute YES/NO mids, distance from strike, lean, and arb edge.

---

## Resolution

When a window ends, the collector:

1. Waits for Gamma to publish a winner (up to a few minutes)
2. If Gamma is still silent, infers **UP / DOWN** from final oracle vs strike
3. Archives 1s ticks to parquet
4. Seals the window’s candle close

The next window does **not** wait on that step.

---

## Backtests (offline)

Run against stored ticks / parquet — no live API required.

- YES/NO ask-reversal counts (threshold flips, e.g. 90¢)
- BTC grid scan
- Zip export for offline research

See `scripts/backtest.ps1` and `scripts/backtest_btc_grid.ps1`.

---

## Remote history datasets (`/v1`)

A backtest app on another machine can pull **one period and one market** (or several) as a single zip. It does not page `/api/windows`.

Contract: **[docs/history-dataset.md](docs/history-dataset.md)**

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/coverage` | Earliest/latest closed windows per market |
| `POST` | `/v1/datasets` | Build (or reuse) a period zip |
| `GET` | `/v1/datasets/{id}` | `building` / `ready` / `failed` |
| `GET` | `/v1/datasets/{id}/download` | The zip |

`/v1` is public, the same as `/api/windows`. No bearer token. Concurrent zip builds default to 1 (`PM_DATASET_BUILD_WORKERS`); a request may span at most 366 days.

The zip contains `manifest.json`, `windows.parquet`, and daily tick files such as `ticks/asset=BTC/interval=5m/date=2026-10-01.parquet`. Ask prices are cents (0–100). The backtest app filters `complete == true` and a non-null `outcome`, then runs locally.

```powershell
python -m pmanalysis start
python scripts/fetch_dataset.py --url http://<collector>:8000 `
  --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m
```

---

## How to run

```powershell
pip install -e .
python -m pmanalysis start
```

Open **http://127.0.0.1:8000** (or `http://<host>:8000` when `PM_HOST=0.0.0.0`).

| Command | Purpose |
|---------|---------|
| `python -m pmanalysis start` | Collector + web (recommended) |
| `python -m pmanalysis collector` | Collector only |
| `python -m pmanalysis web` | Dashboard only (reads DB) |
| `python -m pmanalysis prune-db -y` | Shrink SQLite; keep windows + parquet |

Useful env vars: `PM_HOST`, `PM_PORT`, `PM_TICK_RETENTION_HOURS` (default 24), `PM_FEED_RECORD` (default off).

VPS: `.\scripts\run-vps.ps1` (bind all interfaces + firewall). Linux: `pm2 start ecosystem.config.js`.

---

## Pages

| Path | Feature |
|------|---------|
| `/` | Live terminal |
| `/market/BTC/5m` | Open terminal on that market |
| `/history` | Catalog, win rates, replay |
| `/status` | Feed health and DB size |
| `/ws/live` | Live WebSocket stream for the terminal |
| `/v1/coverage` | Remote backtest: history span per market |
| `/v1/datasets` | Remote backtest: build/download a period zip |

---

Research tool only. Not financial advice, and it does not send orders to Polymarket.
