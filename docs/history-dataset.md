# History dataset API

Version: `1`

The collector records Polymarket crypto Up/Down windows. A remote backtest app asks for a period and one or more markets, downloads one zip, and runs only against that zip. People can download the same zip by hand from the **History** page (see *Manual download* below).

Base URL is the collector, for example `http://192.168.1.20:8420`. `/v1` is public, the same as `/api/windows`: URL plus JSON, no bearer token. Public downloads cover the **last 30 days** (`PM_DATASET_LOOKBACK_DAYS`, counted from UTC midnight) and one request spans at most 31 days (the 30 full days plus today). Older data is shared on request: requests outside the window get HTTP 400 with the owner contact (`PM_OWNER_CONTACT`). `GET /v1/limits` returns the current window. Concurrent builds are capped by `PM_DATASET_BUILD_WORKERS` (default 1).

Markets are one asset × one interval. A request may include more than one; the zip contains only those markets.

| Asset | Interval | Window length |
|-------|----------|---------------|
| `BTC`, `ETH`, `SOL` | `5m` | 300 seconds |
| `BTC`, `ETH`, `SOL` | `15m` | 900 seconds |

Slug form: `btc-updown-5m-1793082000`. The last number is `start_ts` (Unix seconds).

## What the backtest app does

1. `GET /v1/limits` for the downloadable window (last 30 days), and optionally `GET /v1/coverage` for the earliest and latest closed window per market.
2. User picks start, end, and markets inside that window (example: BTC `5m` only).
3. `POST /v1/datasets` with that period.
4. Poll `GET /v1/datasets/{id}` until `status` is `ready` (about once per second). Status polls do not refresh zip TTL.
5. `GET /v1/datasets/{id}/download` and save the zip.
6. Check `manifest.json` against the request.
7. Load `windows.parquet` and the tick files. Skip windows where `complete` is false or `outcome` is empty.
8. Run the strategy locally. Do not call this API again during the run.

Cache the zip. The cache key is `schema_version + start + end + assets + intervals + data_revision` from the manifest. The same key means the file is still valid.

Collector helper:

```powershell
$env:PM_DATASET_URL = "http://192.168.1.20:8420"
python scripts/fetch_dataset.py --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m --out btc_5m.zip
```

## 0. Limits

```http
GET /v1/limits
```

```json
{"lookback_days": 30, "max_range_days": 31, "earliest_start": "2026-10-01T00:00:00Z",
 "assets": ["BTC", "ETH", "SOL"], "intervals": ["5m", "15m"], "contact": "https://polyscripts.world"}
```

| Field | Meaning |
|-------|---------|
| `lookback_days` | How many days back downloads reach (`PM_DATASET_LOOKBACK_DAYS`) |
| `max_range_days` | Longest `end - start` of one request (the full past days plus today) |
| `earliest_start` | Smallest allowed `start`: UTC midnight, `lookback_days` back. Moves forward every day |
| `assets`, `intervals` | Valid values for `POST /v1/datasets` |
| `contact` | Where to ask for older data or longer ranges (`PM_OWNER_CONTACT`) |

Anything outside this window is refused with `400` (datasets) or `403` (per-round CSV), and the message includes `contact`.

### Manual download (History page)

**History → Download historical data** builds the same zip without code:

1. Tick the markets (BTC / ETH / SOL, 5m / 15m) and pick **From** and **To** dates (UTC, inclusive; the date pickers stop at `earliest_start` and today).
2. **Build & download** posts `/v1/datasets` with `start` = From at 00:00Z and `end` = the day after To at 00:00Z, shows build progress, and starts the download when `ready`.
3. The panel lists what the zip contains and links to the owner for older data.

Each row of the History table also has a **CSV** link (`GET /api/export/{window_id}?format=csv`): one round's 1-second rows with the stored and computed tick columns (here `yes_mid`/`no_mid` are 0–1 probabilities, not cents) plus `arb_edge` (100 − YES+NO) and `lean` (YES mid − NO mid). Rounds older than the window show `—` and the endpoint answers `403`.

## 1. Coverage

```http
GET /v1/coverage?asset=BTC&interval=5m
```

`asset` and `interval` are optional. Omit both to list all six markets.

```json
{
  "schema_version": 1,
  "markets": [
    {
      "asset": "BTC",
      "interval": "5m",
      "first_start_ts": 1780338600,
      "last_closed_start_ts": 1793082000,
      "window_count": 12000,
      "archived_window_count": 11840,
      "unresolved_window_count": 3
    }
  ]
}
```

Times are Unix seconds, UTC. Only windows whose `end_ts` is already in the past are counted. The live window is omitted.

Coverage `archived_window_count` is closed windows already written to parquet. Recent windows still only in SQLite are in `window_count` but not this field. Do **not** treat it as backtest-ready. The zip `complete` flag is the tick-quality test (95% fill, 3s edges, 5s gaps).

## 2. Create a dataset

```http
POST /v1/datasets
Content-Type: application/json
```

```json
{
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-27T00:00:00Z",
  "assets": ["BTC"],
  "intervals": ["5m"]
}
```

Rules:

- `start` is inclusive, `end` is exclusive, both UTC with a timezone (`Z` or an offset).
- A window is included when `start_ts >= start` and `start_ts < end` and `end_ts` is already past.
- `assets` is a non-empty subset of `BTC`, `ETH`, `SOL`.
- `intervals` is a non-empty subset of `5m`, `15m`.
- Order is canonical (`BTC`, `ETH`, `SOL` then `5m`, `15m`), so `["SOL","BTC"]` is stored as `["BTC","SOL"]`. Compare as that order, or as sets.
- `start` must be on or after `GET /v1/limits` → `earliest_start` (UTC midnight, 30 days back), and `end - start` is at most `max_range_days` (31). Otherwise `400` with a message that includes the owner contact.
- Optional `force: true` rebuilds the zip even when the cache key matches.

BTC 5-minute for 26 days is one tick-file series, about 86,000 rows per day. Adding another market adds only that market’s files.

Response `202` when the zip is still being built, `200` when an identical dataset is already ready:

```json
{
  "dataset_id": "ds_8f3a1c0b9e2247a1",
  "status": "building",
  "schema_version": 1,
  "data_revision": "7c2e",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-27T00:00:00Z",
  "assets": ["BTC"],
  "intervals": ["5m"]
}
```

The same body returns the same `dataset_id` while catalog rows, sqlite ticks, and archive files for that range are unchanged. A new outcome, deleted ticks, or a rewritten parquet file changes `data_revision` and issues a new id. That includes a late correction: when Polymarket publishes the official winner for a window that had resolved on our inference, the window's `outcome`/`outcome_source` change and so does `data_revision`.

| HTTP | Meaning |
|------|---------|
| 400 | Bad date, unknown asset or interval, empty `assets`/`intervals`, `end <= start`, range longer than `max_range_days`, or `start` before `earliest_start`. The `detail` text names the limit and the owner contact |

## 3. Status

```http
GET /v1/datasets/ds_8f3a1c0b9e2247a1
```

While building:

```json
{
  "dataset_id": "ds_8f3a1c0b9e2247a1",
  "status": "building",
  "schema_version": 1,
  "data_revision": "7c2e",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-27T00:00:00Z",
  "assets": ["BTC"],
  "intervals": ["5m"],
  "windows_done": 120,
  "windows_total": 7488,
  "tick_rows": 36000
}
```

When ready:

```json
{
  "dataset_id": "ds_8f3a1c0b9e2247a1",
  "status": "ready",
  "schema_version": 1,
  "data_revision": "7c2e",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-27T00:00:00Z",
  "assets": ["BTC"],
  "intervals": ["5m"],
  "window_count": 7488,
  "tick_rows": 2239488,
  "bytes": 48000000,
  "download_path": "/v1/datasets/ds_8f3a1c0b9e2247a1/download"
}
```

`status` is `building`, `ready`, or `failed`. On `failed` the body includes a generic `error` (`"build failed"`); details stay in collector logs. Poll about once per second. Status polls do not rewrite metadata or extend TTL. `404` means the id is unknown or expired (zips unused for 7 days after last **download** / build, `PM_DATASET_TTL_DAYS`).

## 4. Download

```http
GET /v1/datasets/ds_8f3a1c0b9e2247a1/download
```

`200` with `Content-Type: application/zip` and `Content-Disposition: attachment; filename="btc_5m_20261001_20261027.zip"`.

- `409` if still `building`
- `500` if `failed`
- `404` if the id is unknown

Filename:

- One market: `btc_5m_20261001_20261027.zip`
- Several assets, one interval: `btc-eth_5m_...`
- All assets: `all_5m_...` or `all_5m-15m_...`

## Zip layout

BTC 5-minute only:

```text
manifest.json
windows.parquet
ticks/asset=BTC/interval=5m/date=2026-10-01.parquet
ticks/asset=BTC/interval=5m/date=2026-10-02.parquet
...
```

A day file exists only when that UTC date has at least one tick row. The date is the UTC date of the window’s `start_ts`.

`windows.parquet` is every included window, sorted by `start_ts`, then `window_id`. Tick files are sorted by `window_id`, then `ts_ms`. Join ticks to windows on `window_id`.

### manifest.json

```json
{
  "schema_version": 1,
  "dataset_id": "ds_8f3a1c0b9e2247a1",
  "data_revision": "7c2e",
  "generated_at": "2026-10-27T13:00:00Z",
  "start": "2026-10-01T00:00:00Z",
  "end": "2026-10-27T00:00:00Z",
  "assets": ["BTC"],
  "intervals": ["5m"],
  "window_count": 7488,
  "complete_window_count": 7401,
  "unresolved_window_count": 2,
  "tick_rows": 2239488,
  "price_unit": "cents",
  "time_unit": "unix",
  "files": [
    { "path": "windows.parquet", "rows": 7488, "sha256": "..." },
    { "path": "ticks/asset=BTC/interval=5m/date=2026-10-01.parquet", "rows": 86400, "sha256": "..." }
  ],
  "incomplete_window_ids": [101, 102],
  "unresolved_window_ids": [103]
}
```

Refuse the file when `schema_version` is not `1`, when `start`/`end` differ, when canonical `assets`/`intervals` differ, or when a member’s SHA256 does not match `files`.

### windows.parquet

| Column | Type | Meaning |
|--------|------|---------|
| `window_id` | int64 | Stable id on the collector. Join key. |
| `slug` | string | `btc-updown-5m-<start_ts>` |
| `asset` | string | `BTC`, `ETH`, or `SOL` |
| `interval` | string | `5m` or `15m` |
| `start_ts` | int64 | Window open, Unix seconds |
| `end_ts` | int64 | Window close, Unix seconds |
| `strike_price` | float64 | Strike recorded for the window (from Polymarket's question or the opening price). Not the settlement target: use `twap_initial` |
| `opening_oracle_price` | float64 | Recorded opening oracle |
| `final_oracle_price` | float64 | Oracle used at resolution |
| `twap_initial` | float64 | 60s Chainlink TWAP at the open: **the settlement target** (price to beat). Null on older windows |
| `final_twap` | float64 | 60s Chainlink TWAP at the close. Null on older windows |
| `outcome` | string | `UP`, `DOWN`, or null |
| `outcome_source` | string | `gamma` (official winner), `inferred` (`final_twap >= twap_initial`, Gamma silent), or null on older windows |
| `tick_count` | int32 | Rows in the tick files for this window |
| `complete` | bool | True when the 1-second series is whole |

`complete` is true only when all of these hold:

- `tick_count >= 95%` of the window length (285 for 5m, 855 for 15m)
- first tick is within 3 seconds after `start_ts`
- last tick is within 3 seconds before `end_ts`
- no gap between consecutive ticks is larger than 5 seconds

Use `outcome` as the settlement label. `UP` means the 60s Chainlink TWAP at the close (`final_twap`) finished at or above `twap_initial`; ties resolve `UP`. Skip rows where `complete` is false or `outcome` is null. Prefer `outcome_source == "gamma"` when you need official results only: `inferred` windows are re-checked against Polymarket for 24 hours and corrected if they differ.

### Tick files

**Source** says where a value comes from. *Stored* columns were observed live and saved every second. *Computed* columns are derived from stored columns and the window row while the zip is built, so they are always consistent with them (the collector does not store them twice).

| Column | Type | Source | Meaning |
|--------|------|--------|---------|
| `window_id` | int64 | stored | Join to `windows.parquet` |
| `ts_ms` | int64 | stored | Sample time, Unix milliseconds |
| `secs_remaining` | int32 | computed | `end_ts − ts_ms/1000`, floored at 0 |
| `oracle_price` | float64 | stored | Chainlink spot |
| `binance_price` | float64 | stored | Binance spot |
| `twap_oracle` | float64 | stored | 60s Chainlink TWAP from Polymarket RTDS. Null when missing |
| `yes_bid` | float64 | stored | YES best bid, **cents** (0 = no bid) |
| `yes_ask` | float64 | stored | YES best ask, **cents** (0 = no ask) |
| `no_bid` | float64 | stored | NO best bid, **cents** |
| `no_ask` | float64 | stored | NO best ask, **cents** |
| `yes_mid` | float64 | computed | YES bid/ask mid, **cents** (the one side quoted when only one is) |
| `no_mid` | float64 | computed | NO mid, **cents** |
| `combined_ask` | float64 | computed | `yes_ask + no_ask`, cents |
| `yes_spread` | float64 | computed | `yes_ask − yes_bid`, cents (0 when a side is missing) |
| `dist_from_strike` | float64 | computed | Chainlink **spot** − `strike_price` (kept for compatibility; not the settlement measure) |
| `twap_diff` | float64 | computed | `twap_oracle − twap_initial`: the settlement-aligned distance. Use this, not `dist_from_strike` |
| `fair_yes` | float64 | stored | Collector's model fair value for YES, cents (see FEATURES.md → *Fair-value model*) |
| `yes_ask_size` / `no_ask_size` | float64 | stored | Shares at the best ask |
| `yes_bid_size` / `no_bid_size` | float64 | stored | Shares at the best bid |
| `yes_ask_depth` / `no_ask_depth` | float64 | stored | Shares on the ask within 2¢ of the best ask |
| `yes_bid_depth` / `no_bid_depth` | float64 | stored | Shares on the bid within 2¢ of the best bid |
| `yes_last_trade` / `no_last_trade` | float64 | stored | Last fill price, cents |
| `traded_usd` | float64 | stored | USDC traded on YES + NO during this second |

The columns from `twap_diff` down (and the new `windows.parquet` columns) were added without changing `schema_version`. Older clients that ignore unknown columns keep working.

When values are null:

- Book sizes/depth, last trades, `traded_usd` and `fair_yes` are null for windows recorded before the 2026-10-30 release.
- `twap_diff` is null when the window has no `twap_initial`, and `fair_yes` is null while the collector has no opening TWAP for the window (for example it started after the window opened and never saw it).
- `traded_usd` is null on the first second after the collector subscribes to a market.

Ticks are stamped on whole seconds (`ts_ms` is a multiple of 1000) from the 2026-10-30 release on; older rows carry the sub-second time they were sampled.

YES/NO prices are cents from 0 to 100. A 90¢ ask is `90.0`, not `0.90`. Oracle, Binance, strike, and TWAP are USD prices.

One row per collected second. A full 5-minute window has about 300 rows. A full day of BTC 5-minute has about 86,400 rows.

Seconds can be missing: when the collector was down, or when its machine was overloaded and the 1-second sampler skipped a beat. Missing seconds are not filled in, so forward-fill if your strategy needs a dense series, and rely on `complete` (95% fill, ≤5s gaps) to drop windows with large holes.

## Python client

```python
from pathlib import Path
from pmanalysis.datasets.client import fetch_dataset, load_dataset_zip

start = "2026-10-01T00:00:00Z"
end = "2026-10-27T00:00:00Z"
assets = ["BTC"]
intervals = ["5m"]

zip_path = fetch_dataset(
    base_url="http://192.168.1.20:8420",
    start=start,
    end=end,
    assets=assets,
    intervals=intervals,
    dest=Path("datasets/btc_5m.zip"),
)
manifest, windows, ticks = load_dataset_zip(
    zip_path, start=start, end=end, assets=assets, intervals=intervals
)
```

A fill uses `yes_ask` or `no_ask` on that `ts_ms`. Settlement uses `outcome` on the window. `secs_remaining` is the clock for time-based entries.

`load_dataset_zip` canonicalizes market lists, verifies SHA256, and drops incomplete and unresolved windows. `fetch_dataset` reuses `dest` when `dataset_id` and `data_revision` already match. If status or download returns 404 (collector restart, expired id), it waits `--poll` seconds (default 1) then POSTs again until the timeout. Default wait is 3600s (`--timeout` on `scripts/fetch_dataset.py`). Tick cleanup and `force: true` restat archive files so `data_revision` is not stuck on the 60s fingerprint cache.

## Collector operations

```powershell
python -m pmanalysis start
```

Zips land in `data/exports/datasets/<dataset_id>/dataset.zip`. Identical requests reuse the zip. Unused ids are deleted after 7 days.

| Env | Default | Role |
|-----|---------|------|
| `PM_DATASET_TTL_DAYS` | `7` | Delete unused zips |
| `PM_DATASET_LOOKBACK_DAYS` | `30` | How far back public downloads reach (from UTC midnight) |
| `PM_DATASET_MAX_RANGE_DAYS` | lookback + 1 | Max `end - start` |
| `PM_OWNER_CONTACT` | `https://polyscripts.world` | Shown when a request is outside the window |
| `PM_DATASET_READ_WORKERS` | `8` | Parallel parquet reads inside one build |
| `PM_DATASET_BUILD_WORKERS` | `1` | Concurrent zip builds |

Hot 1s ticks stay in SQLite for about 24 hours. Older windows are read from `data/archive/<asset>/<interval>/<slug>.parquet`. The zip columns are the same in both cases.

Storage keeps only the *stored* columns above (about 79 bytes per tick in parquet, zstd). The *computed* columns are added while the zip is built (`pmanalysis/db/tick_fields.py`). Archives written before 2026-10-31 also carry their own copies of the computed columns; those copies are used as they are.

To share data older than the public window, raise `PM_DATASET_LOOKBACK_DAYS` (and optionally `PM_DATASET_MAX_RANGE_DAYS`) on a private collector, or copy the archive files directly.

## Acceptance

- `POST` with `assets: ["BTC"]` and `intervals: ["5m"]` returns a zip whose tick paths are only `ticks/asset=BTC/interval=5m/...`.
- A window that ended inside the range is present. The live window is absent. A window that starts before `start` is absent.
- `yes_ask` of a 90¢ offer is `90.0`, and `yes_mid` with a 89¢ bid is `89.5`.
- A request whose `start` is before `earliest_start` returns `400` with the owner contact.
- After SQLite ticks are pruned, an archived window still appears with the same columns.
- A second identical POST returns the same `dataset_id` and does not rebuild the zip.
- The backtest PC can disconnect after the download and finish the run from the zip.
