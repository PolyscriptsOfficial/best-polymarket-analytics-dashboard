"""Derived indicator calculations."""

from __future__ import annotations


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


def win_rate(outcomes: list[str]) -> float | None:
    resolved = [o for o in outcomes if o in ("UP", "DOWN")]
    if not resolved:
        return None
    up_wins = sum(1 for o in resolved if o == "UP")
    return round(up_wins / len(resolved) * 100, 1)
