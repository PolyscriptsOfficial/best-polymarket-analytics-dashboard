"""Decide whether a window's 1-second series is whole enough to backtest."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pmanalysis.datasets.schema import (
    MAX_FIRST_LAG_MS,
    MAX_GAP_MS,
    MAX_LAST_LEAD_MS,
    MIN_FILL_RATIO,
    expected_seconds,
)


@dataclass(frozen=True, slots=True)
class Completeness:
    tick_count: int
    complete: bool


def evaluate_completeness(
    ts_ms,
    *,
    start_ts: int,
    end_ts: int,
    interval: str,
) -> Completeness:
    """True when the series covers the window with no large holes.

    Rules (all must hold):
    - tick count >= 95% of the window length in seconds
    - first tick within 3s after start_ts
    - last tick within 3s before end_ts
    - no gap between consecutive ticks larger than 5s
    """
    stamps = np.asarray(ts_ms, dtype=np.int64)
    if stamps.size:
        stamps = np.sort(stamps)
    tick_count = int(stamps.size)
    expected = expected_seconds(interval, start_ts, end_ts)
    min_ticks = int(np.ceil(expected * MIN_FILL_RATIO)) if expected else 0
    if tick_count == 0 or tick_count < min_ticks:
        return Completeness(tick_count=tick_count, complete=False)

    start_ms = int(start_ts) * 1000
    end_ms = int(end_ts) * 1000
    first = int(stamps[0])
    last = int(stamps[-1])
    if first < start_ms or first > start_ms + MAX_FIRST_LAG_MS:
        return Completeness(tick_count=tick_count, complete=False)
    if last > end_ms or last < end_ms - MAX_LAST_LEAD_MS:
        return Completeness(tick_count=tick_count, complete=False)
    if tick_count >= 2:
        gaps = np.diff(stamps)
        if bool(np.any(gaps > MAX_GAP_MS)):
            return Completeness(tick_count=tick_count, complete=False)
    return Completeness(tick_count=tick_count, complete=True)
