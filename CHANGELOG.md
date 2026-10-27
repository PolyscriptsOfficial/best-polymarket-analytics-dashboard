# Changelog

All notable product and dashboard changes are listed here. Version tags are optional; entries are grouped by date.

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
