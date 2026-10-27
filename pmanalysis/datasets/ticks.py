"""Load 1s ticks for a window set: SQLite while hot, parquet after archive."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.orm import Session

from pmanalysis import config
from pmanalysis.datasets.schema import SQLITE_TICK_BATCH, TICK_SCHEMA, WindowRow, empty_table
from pmanalysis.db.models import Indicator, Tick
from pmanalysis.export.parquet import archive_path_for

log = logging.getLogger(__name__)

_ARCHIVE_OPTIONAL = {
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
}


def mid_to_cents(value: float | None) -> float | None:
    """Stored mids are 0-1 probability; dataset mids are cents like the asks."""
    if value is None:
        return None
    number = float(value)
    if number != number:
        return None
    if 0 < abs(number) <= 1.0:
        return round(number * 100.0, 2)
    return number


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def table_from_rows(window_id: int, rows: list[dict[str, Any]]) -> pa.Table:
    if not rows:
        table = empty_table(TICK_SCHEMA)
        return table
    payload = {
        "window_id": [window_id] * len(rows),
        "ts_ms": [_int(row.get("ts_ms")) for row in rows],
        "secs_remaining": [_int(row.get("secs_remaining")) for row in rows],
        "oracle_price": [_num(row.get("oracle_price")) for row in rows],
        "binance_price": [_num(row.get("binance_price")) for row in rows],
        "twap_oracle": [_num(row.get("twap_oracle")) for row in rows],
        "yes_bid": [_num(row.get("yes_bid")) or 0.0 for row in rows],
        "yes_ask": [_num(row.get("yes_ask")) or 0.0 for row in rows],
        "no_bid": [_num(row.get("no_bid")) or 0.0 for row in rows],
        "no_ask": [_num(row.get("no_ask")) or 0.0 for row in rows],
        "yes_mid": [mid_to_cents(_num(row.get("yes_mid"))) for row in rows],
        "no_mid": [mid_to_cents(_num(row.get("no_mid"))) for row in rows],
        "combined_ask": [_num(row.get("combined_ask")) or 0.0 for row in rows],
        "yes_spread": [_num(row.get("yes_spread")) or 0.0 for row in rows],
        "dist_from_strike": [_num(row.get("dist_from_strike")) for row in rows],
    }
    return pa.Table.from_pydict(payload, schema=TICK_SCHEMA)


def _archive_to_rows(window: WindowRow, table: pa.Table) -> list[dict[str, Any]]:
    names = set(table.column_names)
    frame = table.to_pydict()
    n = table.num_rows
    rows: list[dict[str, Any]] = []
    for i in range(n):
        row = {}
        for field in _ARCHIVE_OPTIONAL:
            if field in names:
                row[field] = frame[field][i]
        rows.append(row)
    rows.sort(key=lambda item: _int(item.get("ts_ms")))
    return rows


def _read_archive(window: WindowRow) -> pa.Table:
    path = archive_path_for(window.asset, window.interval, window.slug)
    if not path.exists():
        log.warning("missing archive %s slug=%s", path, window.slug)
        return empty_table(TICK_SCHEMA)
    try:
        parquet = pq.ParquetFile(path)
        available = [name for name in parquet.schema_arrow.names if name in _ARCHIVE_OPTIONAL]
        table = parquet.read(columns=available or None)
    except Exception:
        log.warning("corrupt archive %s slug=%s", path, window.slug, exc_info=True)
        return empty_table(TICK_SCHEMA)
    return table_from_rows(window.window_id, _archive_to_rows(window, table))


def load_archive_tables(windows: list[WindowRow]) -> dict[int, pa.Table]:
    if not windows:
        return {}
    workers = max(
        1,
        min(
            len(windows),
            config.DATASET_READ_WORKERS // max(1, config.DATASET_BUILD_WORKERS),
        ),
    )
    out: dict[int, pa.Table] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ds-pq") as pool:
        futures = {pool.submit(_read_archive, window): window.window_id for window in windows}
        for future in as_completed(futures):
            out[futures[future]] = future.result()
    return out


def load_sqlite_tables(session: Session, windows: list[WindowRow]) -> dict[int, pa.Table]:
    if not windows:
        return {}
    window_ids = [window.window_id for window in windows]
    ticks_by_window: dict[int, list[dict[str, Any]]] = {wid: [] for wid in window_ids}
    twap_by_key: dict[tuple[int, int], float] = {}

    for offset in range(0, len(window_ids), SQLITE_TICK_BATCH):
        batch = window_ids[offset : offset + SQLITE_TICK_BATCH]
        twap_rows = (
            session.query(Indicator.window_id, Indicator.ts_ms, Indicator.twap_oracle)
            .filter(Indicator.window_id.in_(batch))
            .all()
        )
        for window_id, ts_ms, twap in twap_rows:
            number = _num(twap)
            if number is not None:
                twap_by_key[(int(window_id), int(ts_ms))] = number

        tick_rows = (
            session.query(
                Tick.window_id,
                Tick.ts_ms,
                Tick.secs_remaining,
                Tick.oracle_price,
                Tick.binance_price,
                Tick.yes_bid,
                Tick.yes_ask,
                Tick.no_bid,
                Tick.no_ask,
                Tick.yes_mid,
                Tick.no_mid,
                Tick.combined_ask,
                Tick.yes_spread,
                Tick.dist_from_strike,
            )
            .filter(Tick.window_id.in_(batch))
            .order_by(Tick.window_id, Tick.ts_ms)
            .all()
        )
        for row in tick_rows:
            window_id = int(row[0])
            ts_ms = int(row[1])
            ticks_by_window.setdefault(window_id, []).append(
                {
                    "ts_ms": ts_ms,
                    "secs_remaining": row[2],
                    "oracle_price": row[3],
                    "binance_price": row[4],
                    "twap_oracle": twap_by_key.get((window_id, ts_ms)),
                    "yes_bid": row[5],
                    "yes_ask": row[6],
                    "no_bid": row[7],
                    "no_ask": row[8],
                    "yes_mid": row[9],
                    "no_mid": row[10],
                    "combined_ask": row[11],
                    "yes_spread": row[12],
                    "dist_from_strike": row[13],
                }
            )

    return {
        window.window_id: table_from_rows(
            window.window_id, ticks_by_window.get(window.window_id, [])
        )
        for window in windows
    }


def load_window_tables(session: Session, windows: list[WindowRow]) -> dict[int, pa.Table]:
    """SQLite for live/recent windows; parquet for archived ones."""
    hot = [window for window in windows if not window.archived]
    cold = [window for window in windows if window.archived]
    tables = load_sqlite_tables(session, hot)
    tables.update(load_archive_tables(cold))
    return tables
