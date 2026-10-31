"""1-second window series: SQLite while hot, parquet after retention.

The live dashboard and History replay share this loader so pruning SQLite
ticks never blanks a chart that was already archived.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pmanalysis.db.models import Tick, Window
from pmanalysis.db.tick_fields import with_derived
from pmanalysis.export.parquet import load_window_frame

# Chart payload keys: stored (prices, quotes, TWAP, fair) + derived (mids, YES+NO, gaps).
_SERIES_KEYS = (
    "ts_ms",
    "oracle_price",
    "binance_price",
    "yes_mid",
    "no_mid",
    "yes_ask",
    "no_ask",
    "combined_ask",
    "dist_from_strike",
    "arb_edge",
    "twap_oracle",
    "twap_diff",
    "fair_yes",
)
# Stored columns the series needs (yes/no bids feed the derived mids).
_STORED_KEYS = (
    "oracle_price",
    "binance_price",
    "twap_oracle",
    "yes_ask",
    "yes_bid",
    "no_ask",
    "no_bid",
    "fair_yes",
)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def downsample_rows(rows: list[dict], max_points: int) -> list[dict]:
    if len(rows) <= max_points:
        return list(rows)
    step = max(1, len(rows) // max_points)
    sampled = list(rows[::step])
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _series(window: Window, rows: list[dict]) -> list[dict]:
    rows = with_derived(
        rows,
        end_ts=window.end_ts,
        strike_price=window.strike_price,
        twap_initial=window.twap_initial,
    )
    return [{key: row.get(key) for key in _SERIES_KEYS} for row in rows]


def _rows_from_sqlite(session: Session, window: Window) -> list[dict]:
    ticks = (
        session.query(Tick.ts_ms, *(getattr(Tick, key) for key in _STORED_KEYS))
        .filter_by(window_id=window.id)
        .order_by(Tick.ts_ms)
        .all()
    )
    return _series(window, [dict(zip(("ts_ms", *_STORED_KEYS), tick)) for tick in ticks])


def _rows_from_parquet(window: Window) -> list[dict]:
    frame = load_window_frame(window)
    if frame is None or frame.empty:
        return []
    rows: list[dict] = []
    # Old archives also carry derived columns; with_derived keeps those values.
    keys = [k for k in dict.fromkeys((*_STORED_KEYS, *_SERIES_KEYS[1:])) if k in frame.columns]
    for record in frame[["ts_ms", *keys]].to_dict(orient="records"):
        ts_ms = _int(record.get("ts_ms"))
        if ts_ms is None:
            continue
        row = {key: _num(record.get(key)) for key in keys}
        row["ts_ms"] = ts_ms
        rows.append(row)
    rows.sort(key=lambda row: row["ts_ms"])
    return _series(window, rows)


def load_window_series(session: Session, window: Window) -> list[dict]:
    """SQLite first; parquet if this window was archived and ticks were pruned."""
    rows = _rows_from_sqlite(session, window)
    if rows:
        return rows
    if window.archived:
        return _rows_from_parquet(window)
    return []


def preview_points(rows: list[dict], max_points: int) -> list[dict]:
    keys = ("ts_ms", "yes_ask", "no_ask", "yes_mid", "no_mid")
    slim = [{key: row.get(key) for key in keys} for row in rows]
    return downsample_rows(slim, max_points)


def tick_payload_points(rows: list[dict]) -> list[dict]:
    return [{key: row.get(key) for key in _SERIES_KEYS} for row in rows]
