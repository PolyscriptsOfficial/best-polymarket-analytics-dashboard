"""Fair value of YES for a Chainlink-TWAP Up/Down window.

Settlement: UP when the 60s Chainlink TWAP at window end is >= the target
(the TWAP at window open). We model Chainlink spot as driftless Brownian motion
with per-second dollar volatility ``sigma`` estimated from recent 1s returns.

Let tau = seconds left and W = TWAP lookback (60s). The settling TWAP is the
average spot over [end - W, end].

* tau >= W: the whole averaging window is in the future. The average of a
  Brownian path over [a, a + W] measured from now has mean S (spot) and
  variance sigma^2 * (a + W/3) with a = tau - W, i.e. sigma^2 * (tau - 2W/3).
* tau < W: (W - tau) seconds of the average are already known (``known_avg``);
  the remaining tau seconds average to S with variance sigma^2 * tau / 3, so
  final = ((W - tau) * known_avg + tau * S) / W and the variance is scaled by
  (tau / W)^2.

P(UP) = Phi((mean - target) / sd). This ignores drift and fat tails; the
calibration page measures how well it holds up on stored windows.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

_SQRT2 = math.sqrt(2.0)
# Never quote certainty from a model; leaves room for oracle quirks.
_P_FLOOR = 0.001
_P_CEIL = 0.999


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


def sigma_per_sec(prices: Sequence[float | None], min_samples: int = 30) -> float | None:
    """Per-second dollar volatility from a dense 1s price series.

    Uses the root-mean-square of 1s log returns (zero-mean: more robust than a
    sample stdev over short windows that trend), scaled by the latest price.
    """
    total = 0.0
    count = 0
    prev: float | None = None
    last: float | None = None
    for price in prices:
        if price is None or price <= 0:
            continue
        if prev is not None:
            r = math.log(price / prev)
            total += r * r
            count += 1
        prev = price
        last = price
    if count < min_samples or last is None:
        return None
    return math.sqrt(total / count) * last


def prob_up(
    *,
    spot: float | None,
    target: float | None,
    secs_remaining: float,
    sigma: float | None,
    known_avg: float | None = None,
    twap_window: int = 60,
) -> float | None:
    """Model probability (0-1) that the settling TWAP finishes >= target."""
    if spot is None or target is None or sigma is None or spot <= 0 or target <= 0:
        return None
    tau = max(0.0, float(secs_remaining))
    w = float(twap_window)
    if tau >= w:
        mean = spot
        var = sigma * sigma * (tau - 2.0 * w / 3.0)
    else:
        known = known_avg if known_avg is not None and known_avg > 0 else spot
        frac = tau / w
        mean = (1.0 - frac) * known + frac * spot
        var = sigma * sigma * (tau / 3.0) * frac * frac
    if var <= 0:
        p = 1.0 if mean >= target else 0.0
    else:
        p = _norm_cdf((mean - target) / math.sqrt(var))
    return min(_P_CEIL, max(_P_FLOOR, p))


def fair_yes_cents(**kwargs) -> float | None:
    p = prob_up(**kwargs)
    return None if p is None else round(p * 100.0, 1)


def edges(fair_yes: float | None, yes_ask: float, no_ask: float) -> tuple[float | None, float | None]:
    """(YES edge, NO edge) in cents: fair value minus what it costs to buy."""
    if fair_yes is None:
        return None, None
    edge_yes = round(fair_yes - yes_ask, 1) if yes_ask > 0 else None
    edge_no = round((100.0 - fair_yes) - no_ask, 1) if no_ask > 0 else None
    return edge_yes, edge_no
