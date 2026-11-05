# PolyScripts — product features

PolyScripts is a live analysis terminal for **Polymarket crypto Up or Down** markets. It watches BTC, ETH, and SOL on **5-minute** and **15-minute** windows, records every window, and lets you reopen past windows on the Terminal and download history for backtests.

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
- Green / red tint follows whether the current 60s TWAP is above or below the target (initial TWAP)

### Window title

The market name and the window clock sit on one line, for example **BTC Up or Down · 5m** then `October 27, 3:00AM–3:05AM ET`.

### Live HUD

- **Initial TWAP** — Chainlink TWAP at window open (the price to beat)
- **Chainlink** — current oracle spot
- **TWAP 60s** — live Chainlink TWAP
- **Difference** — current TWAP − initial TWAP
- **Countdown** — time left in the clock bucket (keeps running across rolls)

Top ticker shows BTC / ETH / SOL spot plus the 5m TWAP difference.

### Fair value, book and lead strip

A row under the HUD:

- **Fair YES**: the model's probability, in cents, that the settling 60s TWAP finishes at or above the target (see *Fair-value model* below)
- **YES edge / NO edge**: fair value minus the ask on each side. Positive (green) means the model thinks that side is cheap
- **σ /√s**: Chainlink realized volatility in dollars per √second (15-minute lookback)
- **YES / NO ask size · depth**: shares at the best ask · shares within 2¢ of it
- **Volume**: USDC traded on YES + NO this window
- **Binance lead**: how many seconds Chainlink trails Binance (best return correlation over 10 minutes), the correlation, and the Binance − Chainlink basis
- **Alerts** toggle (see *Alerts*)

Every HUD and strip readout has an info button (ⓘ). Hover, focus or tap it for what the number is and how it helps a trade decision.

### Price chart

Lightweight Charts of the **current window**. Colors:

| Series | Color |
|--------|--------|
| Binance spot | Gold |
| Chainlink | Blue |
| 60s TWAP | Cyan |
| **TWAP Target** (dashed) | Same value as **Initial TWAP** in the HUD (window-open Chainlink TWAP, not the live 60s TWAP) |

Time axis is elapsed time inside that window (0:00 → 5:00 or 15:00), labeled in that elapsed clock (0:00, 0:30, 1:00, …).

On open, both the price chart and the probability chart show the **whole window**. You can still drag or scroll to zoom; double-click the plot to fit the window again.

Live lines advance every **200 ms** so the path stays smooth. Stored ticks stay **1 second** — that is the grain for History, indicators, and backtests. The 200 ms draw forward-fills between those samples.

### Live chart behavior

- Switching **asset** or **interval** (e.g. BTC 5m → BTC 15m) reloads the current clock window’s tick history and fits the full window, not only the latest live point.
- After a **clock roll**, series reset, history is fetched again, the view fits the new bucket, and the TWAP target updates when the new window’s initial TWAP is known (from live market cards or stored ticks).
- Replaying an older slug from the dropdown or sidebar loads that window only; live mode ignores ticks whose `start_ts` is before the current clock bucket.

### Probability chart

YES ask and NO ask from the Polymarket CLOB, in cents (0–100), plus the model **Fair YES** as a dashed amber line (also when reopening past windows recorded with it).

### UP / DOWN candle chart

Sits **under** the YES/NO chart.

- 5m market → **one 5-minute candle per window**
- 15m market → **one 15-minute candle per window**
- Open = initial TWAP (price to beat)
- High / low / close = oracle range in that window
- **Green = UP**, **red = DOWN**
- The last candle updates live
- Click a candle to load that window’s tick charts
- Axis and tooltip times are **Eastern Time**, the same clock as the window range next to the market name
- When the window resolves, the recorded open (initial TWAP) is kept and the close is pinned to the final oracle

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

## Fair-value model

Chainlink spot is modelled as driftless Brownian motion with per-second dollar volatility σ (RMS of 1s log returns over the last 15 minutes). With τ seconds left and W = 60s:

- τ ≥ W: the whole settling average is in the future → mean = spot, variance = σ²(τ − 2W/3)
- τ < W: (W − τ) seconds of the average are already known → mean blends the known average with spot, variance = σ²·τ/3·(τ/W)²

Fair YES = Φ((mean − target) / sd), clamped to 0.1–99.9%. Ties resolve UP. It ignores drift and fat tails, so treat it as a second opinion: the Research → Calibration chart shows how it scores against the market mid on stored windows.

---

## Grid (`/grid`)

All six markets on one screen, updating live: countdown, YES / NO ask, Fair YES, TWAP diff, best edge (side + cents), YES + NO, ask depth, volume and target. The top border is green / red by the sign of the TWAP diff. Click a card to open it in the terminal. The header shows Binance lead per asset.

---

## Alerts

The collector checks every 1s sample:

| Alert | Fires when |
|-------|------------|
| Edge | YES or NO edge ≥ `PM_ALERT_EDGE_CENTS` (default 10¢) with ≥ `PM_ALERT_MIN_SECS_LEFT` (5s) left |
| Arbitrage | YES ask + NO ask ≤ 100 − `PM_ALERT_ARB_CENTS` (default 1¢) |
| Feed gap | Binance / Chainlink / CLOB goes stale or disconnects |

Edge and arbitrage alerts need live inputs: they are skipped while the Chainlink spot is more than 5s old or either side's CLOB quote has not been confirmed for 16s (twice `PM_CLOB_STALE_SEC`). A frozen book or spot would otherwise produce phantom edges; the feed-gap alert covers the outage itself.

Each alert fires at most once per `PM_ALERT_COOLDOWN_SEC` (default 300s) per window and side. Delivery:

- **Browser**: toggle **Alerts** on the Terminal or Grid. Shows a toast, plus a desktop notification when the tab is in the background (asks permission once)
- **Discord**: set `PM_ALERT_DISCORD_WEBHOOK`
- **Telegram**: set `PM_ALERT_TELEGRAM_TOKEN` and `PM_ALERT_TELEGRAM_CHAT_ID`

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
- Each slug links to that market on Polymarket. To chart a past window, pick it in the Terminal's window dropdown
- Optional raw tabs (Binance / Oracle / CLOB) only if feed-record was on
- **Download historical data**: pick assets, intervals and dates (last 30 days), then download one zip with `windows.parquet`, daily per-second tick files and `manifest.json`. Older data or longer ranges: contact the owner (`PM_OWNER_CONTACT`)
- CSV export per window (rounds from the last 30 days)

Older windows still chart after SQLite ticks are pruned — the UI reads **parquet archives**.

---

## Research (`/research`)

Pick asset, interval, period and a **checkpoint** (seconds before close, T−240s … T−5s; 15m adds T−14m … T−5m).

- **Calibration**: windows bucketed by implied YES at the checkpoint (10¢ buckets), against how often UP actually won, with 95% intervals. Two series: market mid and model fair value, plus each one's Brier score. Points above the diagonal mean UP was underpriced. **Show table** gives the numbers
- **Regimes**: UP win rate by hour (Eastern Time), after 1–4 UP or DOWN results in a row (back-to-back windows only), by realized-volatility tercile, and by first-minute TWAP move quintile. Each with a 95% interval
- **Rule backtest**: buy one side at the checkpoint when its ask is in a range, optionally when the TWAP diff favours it by ≥ $X and the model edge is ≥ Y¢. Side can follow the TWAP diff or be fixed. Includes slippage, taker fee (% of notional) and an optional "best-ask size ≥ shares" fill check. Shows trades, win rate vs break-even rate, P&L, ROI, max drawdown, equity curve and the last 200 trades

All three read the `window_snapshots` table (one row per window per checkpoint, written when a window is archived), so they answer in milliseconds without scanning parquet. For windows archived before this release, run once:

```powershell
python -m pmanalysis backfill-features
```

It is safe to run while the collector is live (it commits one window at a time).

---

## Status (`/status`)

- Feed lag: Binance, Chainlink (RTDS), CLOB, each with a source icon and a state icon (ok, stale, down, waiting, idle)
- Collector running, last 1s tick, last discovery
- Database path and size
- Window / tick counts
- Open and recent **feed gaps** (stale or disconnected sockets)
- **Outcome audit**: how many windows resolved on Gamma's official winner vs our own inference, and every window where the two disagree

---

## Data the collector stores

| Store | Role |
|-------|------|
| SQLite `windows` | Catalog: slug, times, strike, opening price, outcome |
| SQLite `ticks` | Hot 1-second samples for ~**24 hours** (observed values only, see below) |
| SQLite `candles` | One OHLC bar per window (5m or 15m) |
| SQLite `window_snapshots` | Quotes, sizes, TWAP diff and fair value at each checkpoint; kept permanently for Research |
| `data/archive/*.parquet` | Full 1s history after a window is archived |
| Raw Binance / Oracle / CLOB tables | **Off** by default (`PM_FEED_RECORD=0`) |

**Why both SQLite and parquet?** SQLite is the live hot store (small, fast). Parquet is the cold archive so History and candles of windows older than 24h still work without a huge database.

**What a 1-second tick stores**: Chainlink and Binance prices, the live 60s TWAP, YES/NO bid and ask, best-level sizes and 2¢ depth, last trades, USDC traded and the model's fair YES. Everything derivable from those (mids, spread, YES+NO, seconds left, distance to strike, TWAP gap, arbitrage gap, lean) is computed when it is read (`pmanalysis/db/tick_fields.py`), so charts, CSV and `/v1` zips still show it. About 79 bytes per tick in parquet, roughly 15 GB a year for all six markets.

1-second ticks are the analysis grain: HUD, line charts, indicators, and backtests all share that series. Candles are a **window-sized** summary of those ticks, not a second feed.

---

## Live feeds

| Feed | What it is used for |
|------|---------------------|
| Polymarket Gamma | Discover current + next market slugs and tokens; resolution |
| Polymarket CLOB WebSocket | YES/NO best bid/ask (REST book only if the socket goes stale) |
| Polymarket RTDS | Chainlink spot + 60s TWAP |
| Binance spot | Overlay on the price chart |

Merged **1s ticks** also compute YES/NO mids, distance from strike, lean, arb edge, **TWAP diff** (live TWAP − initial TWAP, the settlement measure), fair value and σ. They also store the order book (best-level size and 2¢ depth per side), the last trade price per side and USDC traded that second. Ticks are stamped on whole seconds.

---

## Resolution

When a window ends, the collector:

1. Records the **final TWAP** (60s TWAP at the close, which is the next window's opening TWAP) and infers **UP / DOWN** from final TWAP ≥ initial TWAP
2. Waits for Gamma to publish a winner (up to a few minutes). If it does, that is the outcome (`gamma`), and a disagreement with the inference is flagged as a mismatch
3. If Gamma is still silent, uses the inference (`inferred`) and keeps asking Gamma for 24 hours (`PM_RESOLUTION_RECHECK_HOURS`); a late official winner replaces it and is checked for a mismatch
4. Archives 1s ticks to parquet and writes the window's research checkpoints
5. Seals the window’s candle close

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

`/v1` is public, the same as `/api/windows`. No bearer token. Concurrent zip builds default to 1 (`PM_DATASET_BUILD_WORKERS`). Public downloads cover the **last 30 days** (`PM_DATASET_LOOKBACK_DAYS`); older data is on request via `PM_OWNER_CONTACT`, and the API answers `400` with that contact. `GET /v1/limits` returns the window. The History page has the same download as a form, and per-round CSV export follows the same window.

The zip contains `manifest.json`, `windows.parquet`, and daily tick files such as `ticks/asset=BTC/interval=5m/date=2026-10-01.parquet`. Ask prices are cents (0–100). The backtest app filters `complete == true` and a non-null `outcome`, then runs locally.

```powershell
python -m pmanalysis start
python scripts/fetch_dataset.py --url http://<collector>:8420 `
  --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m
```

---

## How to run

```powershell
pip install -e .
python -m pmanalysis start
```

Open **http://127.0.0.1:8420** (or `http://<host>:8420` when `PM_HOST=0.0.0.0`).

| Command | Purpose |
|---------|---------|
| `python -m pmanalysis start` | Collector + web (recommended) |
| `python -m pmanalysis collector` | Collector only |
| `python -m pmanalysis web` | Dashboard only (reads DB) |
| `python -m pmanalysis prune-db -y` | Shrink SQLite; keep windows + parquet |
| `python -m pmanalysis backfill-features` | Compute Research checkpoints for windows archived before this release (`--limit N` for a partial run) |
| `python -m pmanalysis purge-broken-windows -y` | Delete closed windows with incomplete ticks or no outcome (`--dry-run` to preview, `--strict` for dataset completeness rules) |

If port 8420 is already taken, `start` exits immediately and does not launch a second collector. Stop the other process, or set `PM_PORT`.

Useful env vars: `PM_HOST` (default `0.0.0.0`), `PM_PORT` (default `8420`), `PM_TICK_RETENTION_HOURS` (default 24), `PM_FEED_RECORD` (default off), `PM_DATASET_LOOKBACK_DAYS` (default 30), `PM_OWNER_CONTACT` (default https://polyscripts.world), `PM_DATASET_BUILD_WORKERS` (default 1), `PM_FAIR_VOL_LOOKBACK_SEC` (default 900), and the `PM_ALERT_*` settings above.

The settlement TWAP window is fixed at 60s (a market rule since 2026-08-14), so there is no `PM_TWAP_WINDOW` setting.

Every page has a **?** button: what the terminal is for, and how to pull a `/v1` dataset for an external backtest. The brand links to [polyscripts.world](https://polyscripts.world). Version shown in the UI is **v1.0**.

VPS: `.\scripts\run-vps.ps1` (bind all interfaces + firewall). Linux: `pm2 start ecosystem.config.js`.

---

## Pages

| Path | Feature |
|------|---------|
| `/` | Live terminal |
| `/market/BTC/5m` | Open terminal on that market |
| `/grid` | All six markets live |
| `/history` | Catalog, win rates, downloads |
| `/research` | Calibration, regimes, rule backtest |
| `/status` | Feed health and DB size |
| `/ws/live` | Live WebSocket stream for the terminal |
| `/api/research/*` | Calibration, regimes, backtest, outcome audit (JSON) |
| `/v1/coverage` | Remote backtest: history span per market |
| `/v1/datasets` | Remote backtest: build/download a period zip |

---

Research tool only. Not financial advice, and it does not send orders to Polymarket.
