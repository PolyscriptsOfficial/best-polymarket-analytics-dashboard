"""In-memory live state for WebSocket streaming."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pmanalysis.schemas import MarketCard

HISTORY_MAX = 720


def market_key(asset: str, interval: str) -> str:
    return f"{asset}:{interval}"


@dataclass
class TickPoint:
    ts_ms: int
    oracle_price: float | None
    yes_mid: float
    no_mid: float
    combined_ask: float
    dist_from_strike: float | None
    arb_edge: float | None
    lean: float | None

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms,
            "oracle_price": self.oracle_price,
            "yes_mid": self.yes_mid,
            "no_mid": self.no_mid,
            "combined_ask": self.combined_ask,
            "dist_from_strike": self.dist_from_strike,
            "arb_edge": self.arb_edge,
            "lean": self.lean,
        }


@dataclass
class LiveStore:
    oracle_prices: dict[str, float] = field(default_factory=dict)
    markets: dict[str, MarketCard] = field(default_factory=dict)
    history: dict[str, deque[TickPoint]] = field(default_factory=dict)
    updated_at: datetime | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _history_for(self, key: str) -> deque[TickPoint]:
        if key not in self.history:
            self.history[key] = deque(maxlen=HISTORY_MAX)
        return self.history[key]

    async def push_tick(
        self,
        asset: str,
        interval: str,
        card: MarketCard,
        point: TickPoint,
    ) -> None:
        key = market_key(asset, interval)
        async with self._lock:
            self.markets[key] = card
            self._history_for(key).append(point)
            if point.oracle_price is not None:
                self.oracle_prices[asset] = point.oracle_price
            self.updated_at = datetime.now(timezone.utc)
        self._event.set()

    async def set_oracle_prices(self, oracle: dict[str, float]) -> None:
        if not oracle:
            return
        async with self._lock:
            self.oracle_prices.update(oracle)
            self.updated_at = datetime.now(timezone.utc)
        self._event.set()

    async def set_snapshot(
        self,
        cards: list[MarketCard],
        oracle: dict[str, float],
    ) -> None:
        async with self._lock:
            self.markets = {market_key(c.asset, c.interval): c for c in cards}
            self.oracle_prices = dict(oracle)
            self.updated_at = datetime.now(timezone.utc)
        self._event.set()

    async def load_history(
        self,
        asset: str,
        interval: str,
        points: list[TickPoint],
    ) -> None:
        key = market_key(asset, interval)
        async with self._lock:
            buf = deque(maxlen=HISTORY_MAX)
            buf.extend(points[-HISTORY_MAX:])
            self.history[key] = buf
        self._event.set()

    async def wait_update(self, timeout: float = 2.0) -> None:
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def get_snapshot_payload(self) -> dict:
        async with self._lock:
            return {
                "type": "snapshot",
                "ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
                "oracle_prices": dict(self.oracle_prices),
                "markets": [m.model_dump() for m in self.markets.values()],
            }

    async def get_market_card(self, asset: str, interval: str) -> MarketCard | None:
        async with self._lock:
            return self.markets.get(market_key(asset, interval))

    async def get_history_payload(self, asset: str, interval: str) -> dict:
        key = market_key(asset, interval)
        async with self._lock:
            card = self.markets.get(key)
            points = list(self.history.get(key, []))
        return {
            "type": "history",
            "asset": asset,
            "interval": interval,
            "slug": card.slug if card else "",
            "strike_price": card.strike_price if card else None,
            "points": [p.to_dict() for p in points],
        }

    async def get_tick_payload(
        self,
        asset: str,
        interval: str,
        point: TickPoint,
        card: MarketCard,
    ) -> dict:
        return {
            "type": "tick",
            "asset": asset,
            "interval": interval,
            "market": card.model_dump(),
            "point": point.to_dict(),
        }


_store: LiveStore | None = None


def get_live_store() -> LiveStore:
    global _store
    if _store is None:
        _store = LiveStore()
    return _store
