"""Derived indicator calculations."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


def mid_price(ask: float, bid: float) -> float:
    if ask > 0 and bid > 0:
        return round((ask + bid) / 200, 4)
    if ask > 0:
        return round(ask / 100, 4)
    if bid > 0:
        return round(bid / 100, 4)
    return 0.0


def compute_tick_fields(
    *,
    yes_ask: float,
    yes_bid: float,
    no_ask: float,
    no_bid: float,
    oracle_price: float | None,
    strike_price: float | None,
    secs_remaining: int,
) -> dict:
    yes_mid = mid_price(yes_ask, yes_bid)
    no_mid = mid_price(no_ask, no_bid)
    combined_ask = round(yes_ask + no_ask, 1)
    yes_spread = round(yes_ask - yes_bid, 1) if yes_ask and yes_bid else 0.0
    dist = None
    if oracle_price is not None and strike_price is not None:
        dist = round(oracle_price - strike_price, 2)
    return {
        "yes_mid": yes_mid,
        "no_mid": no_mid,
        "combined_ask": combined_ask,
        "yes_spread": yes_spread,
        "secs_remaining": secs_remaining,
        "dist_from_strike": dist,
    }


@dataclass
class IndicatorState:
    oracle_history: list[tuple[int, float]] = field(default_factory=list)

    def add_oracle(self, ts_ms: int, price: float) -> None:
        self.oracle_history.append((ts_ms, price))
        if len(self.oracle_history) > 600:
            self.oracle_history = self.oracle_history[-600:]

    def compute(
        self,
        *,
        ts_ms: int,
        yes_mid: float,
        no_mid: float,
        combined_ask: float,
        window_duration: int,
        secs_remaining: int,
    ) -> dict:
        momentum_1m = None
        volatility_5m = None
        cutoff_1m = ts_ms - 60_000
        cutoff_5m = ts_ms - 300_000
        recent_1m = [p for t, p in self.oracle_history if t >= cutoff_1m]
        recent_5m = [p for t, p in self.oracle_history if t >= cutoff_5m]
        if len(recent_1m) >= 2:
            momentum_1m = round(recent_1m[-1] - recent_1m[0], 2)
        if len(recent_5m) >= 3:
            volatility_5m = round(statistics.pstdev(recent_5m), 4)

        elapsed = max(0, window_duration - secs_remaining)
        time_decay = round(elapsed / window_duration, 4) if window_duration > 0 else 0.0

        return {
            "implied_yes_prob": yes_mid,
            "arb_edge": round(100 - combined_ask, 2),
            "lean": round(yes_mid - no_mid, 4),
            "momentum_1m": momentum_1m,
            "volatility_5m": volatility_5m,
            "time_decay_factor": time_decay,
        }


def compute_indicators(
    state: IndicatorState,
    *,
    ts_ms: int,
    oracle_price: float | None,
    yes_mid: float,
    no_mid: float,
    combined_ask: float,
    window_duration: int,
    secs_remaining: int,
) -> dict:
    if oracle_price is not None:
        state.add_oracle(ts_ms, oracle_price)
    return state.compute(
        ts_ms=ts_ms,
        yes_mid=yes_mid,
        no_mid=no_mid,
        combined_ask=combined_ask,
        window_duration=window_duration,
        secs_remaining=secs_remaining,
    )


def win_rate(outcomes: list[str]) -> float | None:
    resolved = [o for o in outcomes if o in ("UP", "DOWN")]
    if not resolved:
        return None
    up_wins = sum(1 for o in resolved if o == "UP")
    return round(up_wins / len(resolved) * 100, 1)
