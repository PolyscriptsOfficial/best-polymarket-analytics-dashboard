# Changelog

All notable product and dashboard changes are listed here. Version tags are optional; entries are grouped by date.

## 2026-11-02

### Live charts
- **Flat YES/NO and price lines after a roll fixed** (correct again only after a refresh). Live paints were scheduled with `requestAnimationFrame`, which does not fire while the tab is hidden, minimized or fully covered by another window; one pending frame blocked every later paint, and since the roll stops the paint loop nothing restarted it. When the tab came back, the missed seconds were forward-filled with the *current* quote, drawing a flat line at the latest price over a stretch where the market actually moved. Now: paints use a timer while hidden; a short gap is filled with the values the lines already held; a gap longer than 10 s (and every return to the tab) reloads the window's stored 1s ticks, the same data a refresh shows.

### Recent markets sidebar
- **LIVE preview stayed empty (0 ticks) after a roll.** The cards were fetched once, 0.3 s after the roll, when the new window had no ticks yet and the previous one was still `OPEN`. The newest page now refreshes every 10 s while the tab is visible (and on return to the tab), updating charts, tick counts and outcome badges in place without rebuilding the cards.

## 2026-11-01

### Terminal
- **Wrong Initial TWAP on past windows fixed.** Opening an older slug (dropdown, sidebar or candle click) showed the live round's Initial TWAP, so the HUD Difference, the TWAP Target line and the jump at 0:00 on the TWAP line were all measured against the wrong target (e.g. +$37 on a window that resolved DOWN). A past window now uses its own stored target; the live card's value is used only for the live slug, and the first TWAP tick is the fallback when nothing was stored.
- `GET /api/windows/{slug}/ticks` now returns the window's `twap_initial` and `final_twap`.

### Deployment
- **PM2**: new `ecosystem.config.js` runs `python -m pmanalysis start` as app `pmanalysis` (`pm2 start ecosystem.config.js`). Uses the project `.venv` when present, otherwise system Python on Windows and `.venv/bin/python` on Linux. Pins the small-VPS env values below, restarts on crash (`max_restarts` 20, `min_uptime` 5s), timestamps log lines and restarts the process above 700 MB RSS instead of letting the OS OOM-kill it. Do not `pm2 start pmanalysis`: PM2 then treats the package folder as a Node script and crash-loops with `MODULE_NOT_FOUND`. Python logging goes to stderr, so normal INFO lines are in `pmanalysis-error.log`.
- **Listens on all interfaces by default** (`PM_HOST` defaults to `0.0.0.0`; set `127.0.0.1` for local only). `scripts/run-vps.ps1` opens the Windows firewall for `PM_PORT`, prints every reachable URL and starts collector + web.
- Scripts use **system Python, no venv** (`scripts/_env.ps1`; `-Install` runs `pip install -e .`). `run.ps1` / `web.ps1` / `run-vps.ps1` default `PM_PORT=8420` and `PM_FEED_RECORD=0`.

### Collector performance (small VPS)
- **REST `/book` polling is a stale-only fallback.** It used to poll every book at 2 Hz in parallel with the WebSocket, racing the CLOB reader. It now refreshes only tokens with no quote for `PM_CLOB_STALE_SEC` (default 8 s), checked every `PM_BOOK_POLL_SEC` (default 3 s).
- **Lower push rates**: quote push heartbeat `PM_QUOTE_PUSH_SEC` 0.25 s (was ~62 Hz) and live UI push `PM_LIVE_PUSH_MS` 150 ms. CLOB WebSocket updates are still pushed as they arrive; the loop is only a heartbeat. Together these stop the CPU pinning that caused CLOB "slow consumer" disconnects, and the multi-GB PM2 error log from unthrottled httpx INFO lines.

### Datasets
- `scripts/fetch_dataset.py` gains `--timeout` (default 3600 s, or `PM_DATASET_TIMEOUT`) and `--poll` (default 1 s).

## 2026-10-31

### Storage
- **Leaner 1-second ticks**: each second now stores only observed values: Chainlink/Binance prices, live TWAP, YES/NO bid and ask, book sizes and 2¢ depth, last trades, USDC traded and fair YES. Derivable fields (mids, spread, YES+NO, seconds left, strike distance, TWAP gap, arbitrage gap, lean) are computed on read, so the History charts, CSV and `/v1` zips are unchanged. The separate `indicators` table is folded into `ticks` (one row per second instead of two); momentum, 5-minute volatility, time decay and model σ are no longer stored. Parquet archives use zstd and a fixed float schema. Measured on live data: SQLite −33%, archives −48% (≈29 → ≈15 GB a year). Existing databases migrate automatically on start (≈2–3 s); old archives are read as before.
- **Target and final TWAP accuracy**: the exact boundary TWAP sample can arrive 1–2 s after the open. The round record now follows the live target instead of keeping the first provisional value, and the final TWAP is re-read at finalize.

### Downloads
- **Download historical data** panel on History: pick markets and dates, see what the zip contains, build and download in one click.
- **30-day public window**: `/v1/datasets`, the History download and per-round CSV export only serve the last 30 days (`PM_DATASET_LOOKBACK_DAYS`). Requests outside it get a clear error with the owner contact (`PM_OWNER_CONTACT`). New `GET /v1/limits`.

### Live charts
- **Frozen chart after a window roll (mostly 15m) fixed.** The window-open TWAP arrives 1–2 s after the open, after the TWAP line had already painted a few live points. Inserting it at 0:00 used `series.update()` with a time older than the last point, which throws; the start flag was never set, so every following paint threw too and the price and YES/NO lines stopped for the rest of the window (a refresh rebuilt them). The start point is now rebuilt with `setData`, and a single out-of-order paint is skipped instead of aborting.
- **15m chart with a flat gap after selecting the market fixed.** The server's live chart history was capped at 720 points; live pushes (several per second) plus Binance/Chainlink backfill points evicted most of a 15m window, so the history sent on select covered only the last few minutes (Chainlink backfill made the start look fine). History is now one merged point per second, sized for a full 15m window, and stored ticks from SQLite merge under it instead of replacing it.
- Switching 5m ↔ 15m no longer throws from the candle chart (a live candle older than the previous interval's last bar); that error used to abort the tick handler before the price/YES-NO update.
- A late first paint with no history (slow roll, 5m ↔ 15m switch) now starts the lines at "now" instead of locking painting for the window; an empty history response for the window on screen no longer wipes live lines; the chart re-syncs stored history 6 s and 20 s after a roll.

### UI
- Info buttons (ⓘ) on every HUD and strip readout explain what the number is and how it helps a trade.
- Status page: icons for each feed, feed state, card and section.
- History: removed the window replay and probability charts (past windows still open on the Terminal); slugs link to Polymarket. Removed the top-bar version badge.

## 2026-10-30 (analysis release)

### New

- **Fair value and edge**: a TWAP-settlement model prices YES from spot, target, time left and Chainlink volatility. Shown as Fair YES / YES edge / NO edge in a new strip under the HUD and as a dashed line on the probability chart. See FEATURES.md → *Fair-value model*.
- **Order book and trades**: the CLOB feed keeps a local book per token and records fills. Ticks now store best-level size and 2¢ depth for both sides, last trade price, and USDC traded per second.
- **Binance lead**: rolling estimate of how many seconds Chainlink trails Binance, with return correlation and basis, per asset.
- **Grid** page (`/grid`): all six markets live on one screen.
- **Research** page (`/research`): calibration (market and model vs realized UP rate), regime stats (hour ET, streaks, volatility, opening move) and a rule backtester with fees, slippage and size checks. Backed by a new `window_snapshots` table. Run `python -m pmanalysis backfill-features` once for older windows.
- **Alerts**: edge, YES+NO arbitrage and feed-gap alerts to the browser (toast + notification), Discord and Telegram (`PM_ALERT_*`).
- **Outcome audit** on `/status`.

### Fixes

- **Settlement-aligned distance**: ticks store `twap_diff` (live TWAP − initial TWAP) next to the legacy spot-based `dist_from_strike`. The window's `twap_initial` and `final_twap` are now saved instead of living only in memory.
- **Outcome inference** uses final TWAP ≥ initial TWAP (it compared final spot to strike). Every window records `inferred_outcome` and `outcome_source`; a late Gamma winner replaces an inferred one, and disagreements are flagged (`outcome_mismatch`).
- **Whole-second ticks**: the sampler runs on second boundaries and stamps `ts_ms` and `secs_remaining` from the same clock reading.
- **Volatility / momentum** indicators are time-based from a 1s Chainlink tape. They were fed by both the 1 Hz sampler and every live push, so `volatility_5m` covered only ~2–3 minutes.
- **`PM_TWAP_WINDOW` removed**: it was read but never used; settlement is fixed at 60s.
- Live push and sampler share one snapshot builder (`collector/snapshot.py`); `live_emitter.py` is gone.
- **Alerts on stale data**: edge/arbitrage alerts are skipped while the Chainlink spot or either CLOB quote is stale. Discord and Telegram are sent independently, and delivery errors no longer log the webhook URL or bot token.
- **Late corrections**: when Gamma overrides an inferred outcome, or feature extraction recovers a missed TWAP and re-derives the inference, `resolved_at` is bumped so cached `/v1` datasets rebuild.

### Data contract

- `/v1` datasets add `twap_initial`, `final_twap`, `outcome_source` (windows) and `twap_diff`, `fair_yes`, book sizes/depth, last trades, `traded_usd` (ticks). `schema_version` stays 1 (additive); cached zips are rebuilt once.

### Collector reliability

- **Missing indexes restored**: `compact_live_db` (`prune-db`) built its index copies with invalid SQL (`ON compact.<table>`) and swallowed the error, so every compaction dropped all indexes, including `ticks(window_id, ts_ms)`. Per-window tick reads were full table scans. Compaction now copies indexes under their real names, and startup creates any model-declared index that is missing.
- **Compaction keeps `window_snapshots`**, and clearing the database removes them (they would otherwise attach to reused window ids).
- **CLOB "slow consumer" disconnects**: the Chainlink REST fallback built a new HTTP client every 2s even when idle (~150 ms of TLS setup on the event loop each time), and `fetch_price_to_beat` built one per call. Both now reuse a client. Parquet archiving and feature extraction at finalize run on the DB worker instead of the event loop.
- **Faster `backfill-features`** (about 7 → 18 windows/s): column queries instead of ORM objects, vectorised row cleaning.
- **Retention sweeps** only delete SQLite ticks for archived windows that still have tick rows (not every catalog id), use larger batches, and skip WAL checkpoint when nothing was removed — fixes 30-minute recording blackouts that grew longer each day.
- **Retention** runs on a background thread instead of the serialized DB worker queue so the 1 Hz sampler is not blocked behind multi-minute purges.
- **Tick sampler** writes a row when Chainlink and/or Binance is available even if CLOB quotes are temporarily empty (still uses last-known CLOB when present).
- **CLOB feed** prunes quote cache for unsubscribed token ids so memory does not grow without bound.

### Live charts

- Price and probability charts open fitted to the **whole window** (0:00 through the bucket end). Drag or scroll still zooms; double-click the plot fits the window again.
- Live lines advance every **200 ms**. Stored ticks stay 1 second.
- **Binance** is gold and **Chainlink** is blue on the price chart, legend, and Chainlink price label. TWAP stays cyan.
- The Eastern Time window range sits on the same line as the market name. Candle axis times use that same clock.

## 2026-10-27

### Live terminal — window rolls and charts

- **TWAP Target** on the price chart now stays aligned with **Initial TWAP** in the HUD when the clock rolls to a new 5m/15m window. The target no longer reuses the previous window’s value while the header already shows the new open TWAP.
- **Window roll**: charts reset for the new bucket, tick history is reloaded from REST/WebSocket, live UP/DOWN candle starts a new bar, slug dropdown and recent previews refresh, and opening/TWAP polling runs until Gamma/RTDS publish window-open values.
- **5m ↔ 15m (and asset) switch**: chart bounds reset before the live socket re-subscribes; in-window history is loaded from `/api/windows/{slug}/ticks` so price and YES/NO lines fill instead of showing only a 0:00 stub plus the current tick.
- **History vs live seed**: WebSocket/REST history is applied when the window has no filled series yet (`hasWindowHistory`), not merely when any point exists (which blocked backfill after an empty reset).
- **Stale market cards** at roll: clock-fresh stub markets clear old quotes/TWAP open fields; cards from the previous bucket are not merged into the live HUD.
- **Metrics**: Chainlink spot in the HUD uses the same blue accent as the Chainlink chart line.

### API and ops (earlier in this release cycle)

- **`/v1` datasets**: public endpoints (no bearer token); see [docs/history-dataset.md](docs/history-dataset.md).
- **Clear database** removed from UI/CLI (use `prune-db` for retention).
- **404 retry** on dataset fetch respects collector poll interval.
- **Live-store history buffer** increased so a full 15m window fits in memory for late WebSocket subscribers.

### UI

- **Branding**: PolyScripts in the top bar links to [polyscripts.world](https://polyscripts.world); **v1.0** badge; sidebar footer on the terminal (**PolyScripts · v1.0 · by PolyScripts**).
- **Help (`?`)** on all pages: what the app is, how traders use the terminal, and how to connect **`/v1` dataset API** for external backtests.

### Docs

- [FEATURES.md](FEATURES.md) — expanded **Window rolls** and **Live chart behavior**.
- This file — commit-ready summary of dashboard fixes.
