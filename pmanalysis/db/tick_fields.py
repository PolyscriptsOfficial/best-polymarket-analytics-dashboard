"""Which per-second values are stored, and how the rest are derived on read.

Storage rule: a tick row keeps only what was observed live and cannot be
rebuilt later (feeds, quotes, book, trades) plus the model's fair value.
Everything that is arithmetic on those columns and the window record is
derived here, so charts, datasets and backtests see the same values the live
dashboard computed, without storing them twice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pmanalysis.indicators.compute import compute_tick_fields

STORED_TICK_COLUMNS = (
    "oracle_price",
    "binance_price",
    "twap_oracle",
    "yes_ask",
    "yes_bid",
    "no_ask",
    "no_bid",
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
    "fair_yes",
)

# Derived per row from STORED_TICK_COLUMNS + the window (end_ts, strike, target).
DERIVED_TICK_COLUMNS = (
    "yes_mid",  # 0-1 probability (bid/ask mid, else the one side quoted)
    "no_mid",
    "combined_ask",  # YES ask + NO ask, cents
    "yes_spread",
    "secs_remaining",
    "dist_from_strike",  # spot - strike (legacy, spot-based)
    "arb_edge",  # 100 - combined_ask
    "lean",  # yes_mid - no_mid
    "twap_diff",  # live TWAP - window-open TWAP (what settlement compares)
)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # NaN -> None


def derive_tick_fields(
    row: Mapping[str, Any],
    *,
    end_ts: int,
    strike_price: float | None,
    twap_initial: float | None,
) -> dict[str, Any]:
    """DERIVED_TICK_COLUMNS for one stored row (``ts_ms`` in ms, prices in cents)."""
    yes_ask = _num(row.get("yes_ask")) or 0.0
    yes_bid = _num(row.get("yes_bid")) or 0.0
    no_ask = _num(row.get("no_ask")) or 0.0
    no_bid = _num(row.get("no_bid")) or 0.0
    ts_ms = int(row.get("ts_ms") or 0)
    fields = compute_tick_fields(
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=no_ask,
        no_bid=no_bid,
        oracle_price=_num(row.get("oracle_price")),
        strike_price=strike_price,
        secs_remaining=max(0, end_ts - ts_ms // 1000),
    )
    fields["arb_edge"] = round(100 - fields["combined_ask"], 2)
    fields["lean"] = round(fields["yes_mid"] - fields["no_mid"], 4)
    twap = _num(row.get("twap_oracle"))
    fields["twap_diff"] = (
        round(twap - twap_initial, 2) if twap is not None and twap_initial is not None else None
    )
    return fields


def with_derived(
    rows: Iterable[dict[str, Any]],
    *,
    end_ts: int,
    strike_price: float | None,
    twap_initial: float | None,
) -> list[dict[str, Any]]:
    """Add derived fields to each row in place; values already present win.

    Archives written before the storage change carry their own stored copies
    (identical by construction), so they are kept rather than recomputed.
    """
    out = []
    for row in rows:
        for key, value in derive_tick_fields(
            row, end_ts=end_ts, strike_price=strike_price, twap_initial=twap_initial
        ).items():
            if _num(row.get(key)) is None:
                row[key] = value
        out.append(row)
    return out
