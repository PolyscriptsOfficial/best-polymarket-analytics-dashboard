"""History dataset contract: request spec, parquet schemas, completeness knobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyarrow as pa

from pmanalysis import config

SCHEMA_VERSION = 1
# Bumped for additive columns. Old clients only check SCHEMA_VERSION and ignore
# unknown parquet columns; the revision keeps cached zips from predating them.
CONTRACT_REVISION = 2
PRICE_UNIT = "cents"
TIME_UNIT = "unix"

MIN_FILL_RATIO = 0.95
MAX_FIRST_LAG_MS = 3_000
MAX_LAST_LEAD_MS = 3_000
MAX_GAP_MS = 5_000
SQLITE_TICK_BATCH = 250

WINDOW_FIELDS = (
    "window_id",
    "slug",
    "asset",
    "interval",
    "start_ts",
    "end_ts",
    "strike_price",
    "opening_oracle_price",
    "final_oracle_price",
    "twap_initial",
    "final_twap",
    "outcome",
    "outcome_source",
    "tick_count",
    "complete",
)

TICK_FIELDS = (
    "window_id",
    "ts_ms",
    "secs_remaining",
    "oracle_price",
    "binance_price",
    "twap_oracle",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "yes_mid",
    "no_mid",
    "combined_ask",
    "yes_spread",
    "dist_from_strike",
    "twap_diff",
    "fair_yes",
    "yes_ask_size",
    "yes_bid_size",
    "no_ask_size",
    "no_bid_size",
    "yes_ask_depth",
    "yes_bid_depth",
    "no_ask_depth",
    "no_bid_depth",
    "yes_last_trade",
    "no_last_trade",
    "traded_usd",
)

WINDOW_SCHEMA = pa.schema(
    [
        ("window_id", pa.int64()),
        ("slug", pa.string()),
        ("asset", pa.string()),
        ("interval", pa.string()),
        ("start_ts", pa.int64()),
        ("end_ts", pa.int64()),
        ("strike_price", pa.float64()),
        ("opening_oracle_price", pa.float64()),
        ("final_oracle_price", pa.float64()),
        ("twap_initial", pa.float64()),
        ("final_twap", pa.float64()),
        ("outcome", pa.string()),
        ("outcome_source", pa.string()),
        ("tick_count", pa.int32()),
        ("complete", pa.bool_()),
    ]
)

TICK_SCHEMA = pa.schema(
    [
        ("window_id", pa.int64()),
        ("ts_ms", pa.int64()),
        ("secs_remaining", pa.int32()),
        ("oracle_price", pa.float64()),
        ("binance_price", pa.float64()),
        ("twap_oracle", pa.float64()),
        ("yes_bid", pa.float64()),
        ("yes_ask", pa.float64()),
        ("no_bid", pa.float64()),
        ("no_ask", pa.float64()),
        ("yes_mid", pa.float64()),
        ("no_mid", pa.float64()),
        ("combined_ask", pa.float64()),
        ("yes_spread", pa.float64()),
        ("dist_from_strike", pa.float64()),
        ("twap_diff", pa.float64()),
        ("fair_yes", pa.float64()),
        ("yes_ask_size", pa.float64()),
        ("yes_bid_size", pa.float64()),
        ("no_ask_size", pa.float64()),
        ("no_bid_size", pa.float64()),
        ("yes_ask_depth", pa.float64()),
        ("yes_bid_depth", pa.float64()),
        ("no_ask_depth", pa.float64()),
        ("no_bid_depth", pa.float64()),
        ("yes_last_trade", pa.float64()),
        ("no_last_trade", pa.float64()),
        ("traded_usd", pa.float64()),
    ]
)


class SpecError(ValueError):
    """Invalid dataset request (maps to HTTP 400)."""


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    start: datetime
    end: datetime
    assets: tuple[str, ...]
    intervals: tuple[str, ...]
    now_ts: int

    @property
    def start_ts(self) -> int:
        return int(self.start.timestamp())

    @property
    def end_ts(self) -> int:
        return int(self.end.timestamp())

    def iso_start(self) -> str:
        return _iso_z(self.start)

    def iso_end(self) -> str:
        return _iso_z(self.end)


@dataclass(frozen=True, slots=True)
class WindowRow:
    window_id: int
    slug: str
    asset: str
    interval: str
    start_ts: int
    end_ts: int
    strike_price: float | None
    opening_oracle_price: float | None
    final_oracle_price: float | None
    outcome: str | None
    archived: bool
    twap_initial: float | None = None
    final_twap: float | None = None
    outcome_source: str | None = None


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date(start_ts: int) -> str:
    return datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")


def canonical_assets(raw: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    wanted = {item.upper() for item in raw}
    unknown = wanted.difference(config.ASSETS)
    if unknown:
        raise SpecError(f"Unknown asset(s): {', '.join(sorted(unknown))}")
    return tuple(asset for asset in config.ASSETS if asset in wanted)


def canonical_intervals(raw: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    wanted = {item.strip() for item in raw}
    unknown = wanted.difference(config.INTERVALS)
    if unknown:
        raise SpecError(f"Unknown interval(s): {', '.join(sorted(unknown))}")
    return tuple(interval for interval in config.INTERVALS if interval in wanted)


def lookback_start(now: datetime) -> datetime:
    """Earliest downloadable instant: UTC midnight, DATASET_LOOKBACK_DAYS days back."""
    day = (now.astimezone(timezone.utc) - timedelta(days=config.DATASET_LOOKBACK_DAYS)).date()
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def parse_spec(
    *,
    start: datetime,
    end: datetime,
    assets: list[str],
    intervals: list[str],
    now_ts: int | None = None,
) -> DatasetSpec:
    if start.tzinfo is None or end.tzinfo is None:
        raise SpecError("start and end must include a UTC timezone (e.g. ...Z)")
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise SpecError("end must be after start")
    max_days = max(1, config.DATASET_MAX_RANGE_DAYS)
    if end_utc - start_utc > timedelta(days=max_days):
        raise SpecError(
            f"range cannot exceed {max_days} days. For more, contact {config.OWNER_CONTACT}"
        )
    now = datetime.fromtimestamp(
        now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp(), tz=timezone.utc
    )
    earliest = lookback_start(now)
    if start_utc < earliest:
        raise SpecError(
            f"only the last {config.DATASET_LOOKBACK_DAYS} days can be downloaded "
            f"(start must be on or after {earliest.strftime('%Y-%m-%dT%H:%M:%SZ')}). "
            f"For older data, contact {config.OWNER_CONTACT}"
        )
    if not assets:
        raise SpecError("assets must be a non-empty list")
    if not intervals:
        raise SpecError("intervals must be a non-empty list")
    return DatasetSpec(
        start=start_utc,
        end=end_utc,
        assets=canonical_assets(assets),
        intervals=canonical_intervals(intervals),
        now_ts=int(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()),
    )


def empty_table(schema: pa.Schema) -> pa.Table:
    return pa.Table.from_arrays(
        [pa.array([], type=field.type) for field in schema],
        schema=schema,
    )


def expected_seconds(interval: str, start_ts: int, end_ts: int) -> int:
    mapped = config.INTERVAL_SECONDS.get(interval)
    if mapped:
        return mapped
    return max(int(end_ts) - int(start_ts), 0)
