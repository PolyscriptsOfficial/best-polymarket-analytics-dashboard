# History dataset API

Version: `1`

The collector records Polymarket crypto Up/Down windows. A remote backtest app asks for a period and one or more markets, downloads one zip, and runs only against that zip.

Base URL is the collector, for example `http://192.168.1.20:8000`. `/v1` is public, the same as `/api/windows`: URL plus JSON, no bearer token. Load is capped by `PM_DATASET_MAX_RANGE_DAYS` (366) and `PM_DATASET_BUILD_WORKERS` (default 1), not a password.

Markets are one asset × one interval. A request may include more than one; the zip contains only those markets.

| Asset | Interval | Window length |
|-------|----------|---------------|
| `BTC`, `ETH`, `SOL` | `5m` | 300 seconds |
| `BTC`, `ETH`, `SOL` | `15m` | 900 seconds |

Slug form: `btc-updown-5m-1793082000`. The last number is `start_ts` (Unix seconds).

## What the backtest app does

1. Optional: `GET /v1/coverage` to see the earliest and latest closed window for that market.
2. User picks start, end, and markets (example: BTC `5m` only).
3. `POST /v1/datasets` with that period.
4. Poll `GET /v1/datasets/{id}` until `status` is `ready` (about once per second). Status polls do not refresh zip TTL.
5. `GET /v1/datasets/{id}/download` and save the zip.
6. Check `manifest.json` against the request.
7. Load `windows.parquet` and the tick files. Skip windows where `complete` is false or `outcome` is empty.
8. Run the strategy locally. Do not call this API again during the run.

Cache the zip. The cache key is `schema_version + start + end + assets + intervals + data_revision` from the manifest. The same key means the file is still valid.

Collector helper:

```powershell
$env:PM_DATASET_URL = "http://192.168.1.20:8000"
python scripts/fetch_dataset.py --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m --out btc_5m.zip
```

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
- The range is at most 366 days (`PM_DATASET_MAX_RANGE_DAYS`).
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

The same body returns the same `dataset_id` while catalog rows, sqlite ticks, and archive files for that range are unchanged. A new outcome, deleted ticks, or a rewritten parquet file changes `data_revision` and issues a new id.

| HTTP | Meaning |
|------|---------|
| 400 | Bad date, unknown asset or interval, empty `assets`/`intervals`, `end <= start`, range too long |

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
| `strike_price` | float64 | Price to beat (Chainlink TWAP at the open) |
| `opening_oracle_price` | float64 | Recorded opening oracle |
| `final_oracle_price` | float64 | Oracle used at resolution |
| `outcome` | string | `UP`, `DOWN`, or null |
| `tick_count` | int32 | Rows in the tick files for this window |
| `complete` | bool | True when the 1-second series is whole |

`complete` is true only when all of these hold:

- `tick_count >= 95%` of the window length (285 for 5m, 855 for 15m)
- first tick is within 3 seconds after `start_ts`
- last tick is within 3 seconds before `end_ts`
- no gap between consecutive ticks is larger than 5 seconds

Use `outcome` as the settlement label. `UP` means the window finished above `strike_price`. Skip rows where `complete` is false or `outcome` is null.

### Tick files

| Column | Type | Meaning |
|--------|------|---------|
| `window_id` | int64 | Join to `windows.parquet` |
| `ts_ms` | int64 | Sample time, Unix milliseconds |
| `secs_remaining` | int32 | Seconds left in the window at this tick |
| `oracle_price` | float64 | Chainlink spot |
| `binance_price` | float64 | Binance spot |
| `twap_oracle` | float64 | 60s Chainlink TWAP. Null when missing |
| `yes_bid` | float64 | YES best bid, **cents** |
| `yes_ask` | float64 | YES best ask, **cents** |
| `no_bid` | float64 | NO best bid, **cents** |
| `no_ask` | float64 | NO best ask, **cents** |
| `yes_mid` | float64 | YES mid, **cents** |
| `no_mid` | float64 | NO mid, **cents** |
| `combined_ask` | float64 | `yes_ask + no_ask`, cents |
| `yes_spread` | float64 | YES spread, cents |
| `dist_from_strike` | float64 | Oracle distance from `strike_price` |

YES/NO prices are cents from 0 to 100. A 90¢ ask is `90.0`, not `0.90`. Oracle, Binance, strike, and TWAP are USD prices.

One row per collected second. A full 5-minute window has about 300 rows. A full day of BTC 5-minute has about 86,400 rows.

## Python client

```python
from pathlib import Path
from pmanalysis.datasets.client import fetch_dataset, load_dataset_zip

start = "2026-10-01T00:00:00Z"
end = "2026-10-27T00:00:00Z"
assets = ["BTC"]
intervals = ["5m"]

zip_path = fetch_dataset(
    base_url="http://192.168.1.20:8000",
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
| `PM_DATASET_MAX_RANGE_DAYS` | `366` | Max `end - start` |
| `PM_DATASET_READ_WORKERS` | `8` | Parallel parquet reads inside one build |
| `PM_DATASET_BUILD_WORKERS` | `1` | Concurrent zip builds |

Hot 1s ticks stay in SQLite for about 24 hours. Older windows are read from `data/archive/<asset>/<interval>/<slug>.parquet`. The zip columns are the same in both cases.

## Acceptance

- `POST` with `assets: ["BTC"]` and `intervals: ["5m"]` returns a zip whose tick paths are only `ticks/asset=BTC/interval=5m/...`.
- A window that ended inside the range is present. The live window is absent. A window that starts before `start` is absent.
- `yes_ask` of a 90¢ offer is `90.0`.
- After SQLite ticks are pruned, an archived window still appears with the same columns.
- A second identical POST returns the same `dataset_id` and does not rebuild the zip.
- The backtest PC can disconnect after the download and finish the run from the zip.
