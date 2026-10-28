"""Per-asset live analytics on 1s Chainlink/Binance tapes (no DB access)."""

from __future__ import annotations

from pmanalysis.analytics.fair_value import sigma_per_sec
from pmanalysis.analytics.lead_lag import LeadLag, estimate_lead_lag
from pmanalysis.analytics.tape import SecondTape
from pmanalysis.config import (
    ASSETS,
    FAIR_VOL_LOOKBACK_SEC,
    FAIR_VOL_MIN_SAMPLES,
    LEAD_LAG_LOOKBACK_SEC,
    LEAD_LAG_MAX_SEC,
)

_TAPE_SECONDS = max(FAIR_VOL_LOOKBACK_SEC, LEAD_LAG_LOOKBACK_SEC) + 120


class LiveAnalytics:
    """Shared by the 1 Hz sampler and the sub-second live push.

    Every getter is keyed by whole second and cached, so a burst of live
    pushes in the same second reuses one computation per asset.
    """

    def __init__(self, assets: tuple[str, ...] = ASSETS) -> None:
        self.chainlink = {a: SecondTape(_TAPE_SECONDS) for a in assets}
        self.binance = {a: SecondTape(_TAPE_SECONDS) for a in assets}
        self._sigma_cache: dict[str, tuple[int, float | None]] = {}
        self._lead_lag: dict[str, LeadLag] = {}

    def on_chainlink(self, asset: str, ts_ms: int, price: float) -> None:
        tape = self.chainlink.get(asset)
        if tape is not None:
            tape.add(ts_ms, price)

    def on_binance(self, asset: str, ts_ms: int, price: float) -> None:
        tape = self.binance.get(asset)
        if tape is not None:
            tape.add(ts_ms, price)

    def sigma(self, asset: str, now_sec: int) -> float | None:
        """Per-second dollar volatility of Chainlink spot over the lookback."""
        cached = self._sigma_cache.get(asset)
        if cached and cached[0] == now_sec:
            return cached[1]
        tape = self.chainlink.get(asset)
        value = None
        if tape is not None and len(tape) >= FAIR_VOL_MIN_SAMPLES:
            value = sigma_per_sec(
                tape.dense(now_sec - FAIR_VOL_LOOKBACK_SEC, now_sec),
                min_samples=FAIR_VOL_MIN_SAMPLES,
            )
        self._sigma_cache[asset] = (now_sec, value)
        return value

    def chainlink_age(self, asset: str, now_sec: int) -> int | None:
        """Seconds since the last Chainlink price for ``asset`` (None if never seen)."""
        tape = self.chainlink.get(asset)
        last = tape.last_sec() if tape is not None else None
        return None if last is None else max(0, now_sec - last)

    def mean_price(self, asset: str, start_sec: int, end_sec: int) -> float | None:
        """Mean 1s Chainlink price over [start_sec, end_sec] (for the TWAP tail)."""
        tape = self.chainlink.get(asset)
        if tape is None or end_sec < start_sec:
            return None
        values = [p for p in tape.dense(start_sec, end_sec) if p is not None]
        return sum(values) / len(values) if values else None

    def refresh_lead_lag(self, now_sec: int) -> dict[str, dict]:
        start = now_sec - LEAD_LAG_LOOKBACK_SEC
        for asset in self.chainlink:
            result = estimate_lead_lag(
                self.binance[asset].dense(start, now_sec),
                self.chainlink[asset].dense(start, now_sec),
                max_lag=LEAD_LAG_MAX_SEC,
            )
            if result is not None:
                self._lead_lag[asset] = result
        return self.lead_lag_payload()

    def lead_lag_payload(self) -> dict[str, dict]:
        return {asset: row.to_dict() for asset, row in self._lead_lag.items()}
