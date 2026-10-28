"""Binance spot vs Chainlink oracle: basis and lead/lag."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LeadLag:
    lag_sec: int  # Chainlink trails Binance by this many seconds
    corr: float  # return correlation at that lag
    basis: float | None  # Binance - Chainlink, dollars (latest)
    samples: int

    def to_dict(self) -> dict:
        return {
            "lag_sec": self.lag_sec,
            "corr": round(self.corr, 3),
            "basis": None if self.basis is None else round(self.basis, 2),
            "samples": self.samples,
        }


def _returns(prices: Sequence[float | None]) -> list[float | None]:
    out: list[float | None] = [None]
    for prev, cur in zip(prices, prices[1:]):
        if prev is None or cur is None or prev <= 0 or cur <= 0:
            out.append(None)
        else:
            out.append(math.log(cur / prev))
    return out


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def estimate_lead_lag(
    binance: Sequence[float | None],
    chainlink: Sequence[float | None],
    max_lag: int = 10,
    min_samples: int = 60,
) -> LeadLag | None:
    """Lag k (0..max_lag) maximizing corr(binance_ret[t - k], chainlink_ret[t]).

    Both inputs are dense 1s series over the same seconds (see SecondTape.dense).
    """
    n = min(len(binance), len(chainlink))
    if n < min_samples + max_lag:
        return None
    rb = _returns(binance[-n:])
    rc = _returns(chainlink[-n:])
    best: tuple[int, float, int] | None = None
    for k in range(max_lag + 1):
        xs: list[float] = []
        ys: list[float] = []
        for t in range(k, n):
            x = rb[t - k]
            y = rc[t]
            if x is None or y is None:
                continue
            xs.append(x)
            ys.append(y)
        if len(xs) < min_samples:
            continue
        c = _corr(xs, ys)
        if best is None or c > best[1]:
            best = (k, c, len(xs))
    if best is None:
        return None
    last_b = next((p for p in reversed(binance) if p is not None), None)
    last_c = next((p for p in reversed(chainlink) if p is not None), None)
    basis = last_b - last_c if last_b is not None and last_c is not None else None
    return LeadLag(lag_sec=best[0], corr=best[1], basis=basis, samples=best[2])
