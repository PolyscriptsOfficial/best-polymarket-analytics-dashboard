"""1-second window series: SQLite while hot, parquet after retention.

The live dashboard and History replay share this loader so pruning SQLite
ticks never blanks a chart that was already archived.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pmanalysis.db.models import Indicator, Tick, Window
from pmanalysis.export.parquet import load_window_frame

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


def _rows_from_sqlite(session: Session, window: Window) -> list[dict]:
    ticks = (
        session.query(Tick)
        .filter_by(window_id=window.id)
        .order_by(Tick.ts_ms)
        .all()
    )
    if not ticks:
        return []
    indicators = {
        row.ts_ms: row
        for row in session.query(Indicator).filter_by(window_id=window.id).all()
    }
    rows: list[dict] = []
    for tick in ticks:
        indicator = indicators.get(tick.ts_ms)
        rows.append(
            {
                "ts_ms": tick.ts_ms,
                "oracle_price": tick.oracle_price,
                "binance_price": tick.binance_price,
                "yes_mid": tick.yes_mid,
                "no_mid": tick.no_mid,
                "yes_ask": tick.yes_ask,
                "no_ask": tick.no_ask,
                "combined_ask": tick.combined_ask,
                "dist_from_strike": tick.dist_from_strike,
                "arb_edge": indicator.arb_edge if indicator else None,
                "twap_oracle": getattr(indicator, "twap_oracle", None) if indicator else None,
            }
        )
    return rows


def _rows_from_parquet(window: Window) -> list[dict]:
    frame = load_window_frame(window)
    if frame is None or frame.empty:
        return []
    rows: list[dict] = []
    for record in frame.to_dict(orient="records"):
        ts_ms = _int(record.get("ts_ms"))
        if ts_ms is None:
            continue
        rows.append(
            {
                "ts_ms": ts_ms,
                "oracle_price": _num(record.get("oracle_price")),
                "binance_price": _num(record.get("binance_price")),
                "yes_mid": _num(record.get("yes_mid")),
                "no_mid": _num(record.get("no_mid")),
                "yes_ask": _num(record.get("yes_ask")),
                "no_ask": _num(record.get("no_ask")),
                "combined_ask": _num(record.get("combined_ask")),
                "dist_from_strike": _num(record.get("dist_from_strike")),
                "arb_edge": _num(record.get("arb_edge")),
                "twap_oracle": _num(record.get("twap_oracle")),
            }
        )
    rows.sort(key=lambda row: row["ts_ms"])
    return rows


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
