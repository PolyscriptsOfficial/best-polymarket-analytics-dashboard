"""Per-window research features: checkpoint snapshots + window-level stats.

Computed once when a window is archived (and by ``backfill-features`` for older
windows) so research queries never have to scan parquet archives.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from pmanalysis.analytics.fair_value import fair_yes_cents
from pmanalysis.config import (
    FAIR_VOL_MIN_SAMPLES,
    INTERVAL_SECONDS,
    SNAPSHOT_CHECKPOINTS_SEC,
    SNAPSHOT_TOLERANCE_SEC,
    TWAP_WINDOW_SEC,
)

SNAPSHOT_FIELDS = (
    "yes_ask",
    "yes_bid",
    "no_ask",
    "no_bid",
    "yes_ask_size",
    "no_ask_size",
    "oracle_price",
    "binance_price",
    "twap_diff",
    "fair_yes",
)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def checkpoints_for(interval: str) -> tuple[int, ...]:
    duration = INTERVAL_SECONDS[interval]
    return tuple(t for t in SNAPSHOT_CHECKPOINTS_SEC if t < duration)


@dataclass
class WindowFeatures:
    twap_initial: float | None = None
    final_twap: float | None = None
    realized_vol: float | None = None
    open_move: float | None = None
    snapshots: dict[int, dict] = field(default_factory=dict)  # t_minus -> SNAPSHOT_FIELDS


class _Series:
    """Sorted 1s rows with O(log n) lookups and O(1) prefix volatility."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.ts = [r["ts_ms"] for r in rows]
        # Prefix sums of squared 1s log returns of oracle_price.
        self._sq = [0.0]
        self._n = [0]
        prev = None
        for row in rows:
            price = row["oracle_price"]
            sq, n = self._sq[-1], self._n[-1]
            if price is not None and price > 0:
                if prev is not None:
                    r = math.log(price / prev)
                    sq, n = sq + r * r, n + 1
                prev = price
            self._sq.append(sq)
            self._n.append(n)

    def index_at(self, target_ms: int, tolerance_ms: int) -> int | None:
        """Index of the last row at or before target_ms, within the tolerance."""
        i = bisect_right(self.ts, target_ms) - 1
        if i < 0 or target_ms - self.ts[i] > tolerance_ms:
            return None
        return i

    def sigma_through(self, i: int) -> float | None:
        """Per-second dollar vol from returns up to and including row i."""
        n = self._n[i + 1]
        price = next(
            (self.rows[j]["oracle_price"] for j in range(i, -1, -1) if self.rows[j]["oracle_price"]),
            None,
        )
        if n < FAIR_VOL_MIN_SAMPLES or price is None:
            return None
        return math.sqrt(self._sq[i + 1] / n) * price


_ROW_KEYS = (*SNAPSHOT_FIELDS, "twap_oracle", "twap_initial")


def extract_features(
    raw_rows: list[dict],
    *,
    interval: str,
    start_ts: int,
    end_ts: int,
    twap_initial: float | None = None,
) -> WindowFeatures:
    """Features from a window's 1s rows (dicts of tick columns)."""
    rows = []
    for raw in raw_rows:
        ts = _num(raw.get("ts_ms"))
        if ts is None:
            continue
        row = {key: _num(raw.get(key)) for key in _ROW_KEYS}
        row["ts_ms"] = int(ts)
        rows.append(row)
    rows.sort(key=lambda r: r["ts_ms"])
    return _extract(rows, interval=interval, start_ts=start_ts, end_ts=end_ts, twap_initial=twap_initial)


def extract_features_frame(frame: pd.DataFrame, **kwargs) -> WindowFeatures:
    """Same as :func:`extract_features` for a ``window_frame`` DataFrame.

    Cleans columns in bulk (coerce to float, NaN/inf -> None) instead of
    per cell; this is the path used at finalize and by backfill-features.
    """
    data = frame.reindex(columns=("ts_ms", *_ROW_KEYS)).apply(pd.to_numeric, errors="coerce")
    data = data[np.isfinite(data["ts_ms"])].sort_values("ts_ms", kind="stable")
    values = data.to_numpy(dtype=float)
    cells = values.astype(object)
    cells[~np.isfinite(values)] = None
    ts_list = values[:, 0].astype(np.int64).tolist()
    rows = []
    for ts, rest in zip(ts_list, cells[:, 1:].tolist(), strict=True):
        row = dict(zip(_ROW_KEYS, rest, strict=True))
        row["ts_ms"] = ts
        rows.append(row)
    return _extract(rows, **kwargs)


def _extract(
    rows: list[dict],
    *,
    interval: str,
    start_ts: int,
    end_ts: int,
    twap_initial: float | None,
) -> WindowFeatures:
    """Core extraction over clean rows sorted by ts_ms (values float or None)."""
    out = WindowFeatures()
    if not rows:
        out.twap_initial = twap_initial
        return out

    tolerance_ms = SNAPSHOT_TOLERANCE_SEC * 1000
    start_ms, end_ms = start_ts * 1000, end_ts * 1000
    series = _Series(rows)

    def row_at(target_ms: int) -> dict | None:
        i = series.index_at(target_ms, tolerance_ms)
        return None if i is None else rows[i]

    if twap_initial is None:
        twap_initial = next((r["twap_initial"] for r in rows if r["twap_initial"] is not None), None)
    if twap_initial is None:
        first = next((r for r in rows if r["twap_oracle"] is not None), None)
        if first is not None and first["ts_ms"] - start_ms <= tolerance_ms:
            twap_initial = first["twap_oracle"]
    out.twap_initial = twap_initial

    def twap_diff(row: dict) -> float | None:
        if row["twap_diff"] is not None:
            return row["twap_diff"]
        if row["twap_oracle"] is None or twap_initial is None:
            return None
        return round(row["twap_oracle"] - twap_initial, 2)

    close_row = row_at(end_ms)
    if close_row is not None:
        out.final_twap = close_row["twap_oracle"]

    sigma_all = series.sigma_through(len(rows) - 1)
    out.realized_vol = None if sigma_all is None else round(sigma_all, 6)

    open_row = row_at(start_ms + 60_000)
    if open_row is not None:
        out.open_move = twap_diff(open_row)

    tail_start_ms = end_ms - TWAP_WINDOW_SEC * 1000
    for t_minus in checkpoints_for(interval):
        idx = series.index_at(end_ms - t_minus * 1000, tolerance_ms)
        if idx is None:
            continue
        row = rows[idx]
        snap = {key: row[key] for key in SNAPSHOT_FIELDS}
        snap["twap_diff"] = twap_diff(row)
        if snap["fair_yes"] is None:
            # Backfill path (pre-model archives): in-window realized vol only.
            sigma = series.sigma_through(idx)
            tail = []
            if t_minus < TWAP_WINDOW_SEC:
                j = bisect_right(series.ts, tail_start_ms - 1)
                tail = [r["oracle_price"] for r in rows[j : idx + 1] if r["oracle_price"] is not None]
            snap["fair_yes"] = fair_yes_cents(
                spot=row["oracle_price"],
                target=twap_initial,
                secs_remaining=t_minus,
                sigma=sigma,
                known_avg=sum(tail) / len(tail) if tail else None,
                twap_window=TWAP_WINDOW_SEC,
            )
        out.snapshots[t_minus] = snap
    return out
