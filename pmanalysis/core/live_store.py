"""In-memory live state for WebSocket streaming."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from pmanalysis.schemas import MarketCard

# Chart history is kept per wall-clock second, so its size is bounded by time
# (a full 15m window + margin) instead of by how many pushes happened. A count
# cap (720) used to evict most of a 15m window: live pushes arrive several
# times a second and feed backfills added Binance/Chainlink points on top.
HISTORY_MAX_SEC = 15 * 60 + 120
_FEED_FIELDS = ("oracle_price", "binance_price", "twap_oracle")


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
    binance_price: float | None = None
    yes_ask: float = 0.0
    yes_bid: float = 0.0
    no_ask: float = 0.0
    no_bid: float = 0.0
    twap_oracle: float | None = None
    twap_diff: float | None = None
    fair_yes: float | None = None

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms,
            "oracle_price": self.oracle_price,
            "binance_price": self.binance_price,
            "yes_mid": self.yes_mid,
            "no_mid": self.no_mid,
            "yes_ask": self.yes_ask,
            "yes_bid": self.yes_bid,
            "no_ask": self.no_ask,
            "no_bid": self.no_bid,
            "combined_ask": self.combined_ask,
            "dist_from_strike": self.dist_from_strike,
            "arb_edge": self.arb_edge,
            "lean": self.lean,
            "twap_oracle": self.twap_oracle,
            "twap_diff": self.twap_diff,
            "fair_yes": self.fair_yes,
        }


class SecondHistory:
    """One merged TickPoint per second, oldest seconds dropped beyond max_seconds."""

    __slots__ = ("_by_sec", "max_seconds")

    def __init__(self, max_seconds: int = HISTORY_MAX_SEC) -> None:
        self._by_sec: dict[int, TickPoint] = {}
        self.max_seconds = max_seconds

    def __len__(self) -> int:
        return len(self._by_sec)

    def __iter__(self):
        return iter(self.points())

    def points(self) -> list[TickPoint]:
        return [self._by_sec[sec] for sec in sorted(self._by_sec)]

    def add(self, point: TickPoint) -> None:
        """Latest push in a second wins; feed prices it lacks are kept."""
        sec = point.ts_ms // 1000
        prev = self._by_sec.get(sec)
        if prev is not None:
            for name in _FEED_FIELDS:
                if getattr(point, name) is None:
                    setattr(point, name, getattr(prev, name))
        self._by_sec[sec] = point
        self._trim()

    def fill(self, name: str, ts_ms: int, value: float) -> None:
        """Set a feed price for its second without displacing quote data."""
        sec = ts_ms // 1000
        point = self._by_sec.get(sec)
        if point is None:
            self._by_sec[sec] = _feed_point(ts_ms, **{name: value})
            self._trim()
        elif getattr(point, name) is None:
            setattr(point, name, value)

    def _trim(self) -> None:
        if len(self._by_sec) <= self.max_seconds:
            return
        floor = max(self._by_sec) - self.max_seconds
        for sec in [s for s in self._by_sec if s <= floor]:
            del self._by_sec[sec]


def _feed_point(ts_ms: int, **prices: float) -> TickPoint:
    """History stub carrying only feed prices (no quotes yet for that second)."""
    return TickPoint(
        ts_ms=ts_ms,
        oracle_price=prices.get("oracle_price"),
        binance_price=prices.get("binance_price"),
        twap_oracle=prices.get("twap_oracle"),
        yes_mid=0.0,
        no_mid=0.0,
        combined_ask=0.0,
        dist_from_strike=None,
        arb_edge=None,
        lean=None,
    )


@dataclass
class LiveStore:
    oracle_prices: dict[str, float] = field(default_factory=dict)
    binance_prices: dict[str, float] = field(default_factory=dict)
    twap_prices: dict[str, float] = field(default_factory=dict)  # alias: 60s map
    twap_by_window: dict[str, dict[str, float]] = field(
        default_factory=lambda: {"30": {}, "60": {}}
    )
    markets: dict[str, MarketCard] = field(default_factory=dict)
    history: dict[str, SecondHistory] = field(default_factory=dict)
    updated_at: datetime | None = None
    seq: int = 0
    _tick_queue: deque[tuple[int, dict]] = field(default_factory=lambda: deque(maxlen=300))
    # Per-asset Binance->Chainlink lead/lag + basis (analytics/lead_lag.py).
    lead_lag: dict[str, dict] = field(default_factory=dict)
    alert_seq: int = 0
    _alert_queue: deque[tuple[int, dict]] = field(default_factory=lambda: deque(maxlen=50))
    _oracle_dirty: bool = False
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _history_for(self, key: str) -> SecondHistory:
        if key not in self.history:
            self.history[key] = SecondHistory()
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
            prev = self.markets.get(key)
            if prev and prev.slug != card.slug:
                self.history[key] = SecondHistory()
            self.markets[key] = card
            self._history_for(key).add(point)
            if point.oracle_price is not None:
                self.oracle_prices[asset] = point.oracle_price
            if point.binance_price is not None:
                self.binance_prices[asset] = point.binance_price
            self.updated_at = datetime.now(timezone.utc)
            self.seq += 1
            payload = {
                "type": "tick",
                "asset": asset,
                "interval": interval,
                "market": card.model_dump(),
                "point": point.to_dict(),
            }
            self._tick_queue.append((self.seq, payload))
        self._event.set()

    async def push_alert(self, payload: dict) -> None:
        async with self._lock:
            self.alert_seq += 1
            self._alert_queue.append((self.alert_seq, payload))
        self._event.set()

    async def drain_alerts(self, since_seq: int) -> tuple[list[dict], int]:
        async with self._lock:
            return [p for seq, p in self._alert_queue if seq > since_seq], self.alert_seq

    async def set_lead_lag(self, payload: dict[str, dict]) -> None:
        async with self._lock:
            self.lead_lag = dict(payload)
            self._oracle_dirty = True
        self._event.set()

    async def set_oracle_prices(self, oracle: dict[str, float]) -> None:
        if not oracle:
            return
        async with self._lock:
            changed = any(self.oracle_prices.get(k) != v for k, v in oracle.items())
            self.oracle_prices.update(oracle)
            self.updated_at = datetime.now(timezone.utc)
            if changed:
                self._oracle_dirty = True
        self._event.set()

    async def set_twap_prices(self, prices: dict[str, float]) -> None:
        """Legacy: treat as 60s TWAP map."""
        if not prices:
            return
        await self.set_twap_by_window({"60": prices})

    async def set_twap_by_window(self, by_win: dict[str, dict[str, float]]) -> None:
        """Update 30s/60s TWAP maps from RTDS. Keys are '30' / '60'."""
        if not by_win:
            return
        async with self._lock:
            changed = False
            for key in ("30", "60"):
                prices = by_win.get(key) or {}
                if not prices:
                    continue
                bucket = self.twap_by_window.setdefault(key, {})
                if any(bucket.get(k) != v for k, v in prices.items()):
                    changed = True
                bucket.update(prices)
            # Keep twap_prices as 60s for ticker / older clients.
            self.twap_prices = dict(self.twap_by_window.get("60") or {})
            self.updated_at = datetime.now(timezone.utc)
            if changed:
                self._oracle_dirty = True
        self._event.set()

    async def set_binance_prices(self, prices: dict[str, float], *, force: bool = False) -> None:
        if not prices:
            return
        async with self._lock:
            changed = any(self.binance_prices.get(k) != v for k, v in prices.items())
            self.binance_prices.update(prices)
            self.updated_at = datetime.now(timezone.utc)
            if changed or force:
                self._oracle_dirty = True
        self._event.set()

    async def set_snapshot(
        self,
        cards: list[MarketCard],
        oracle: dict[str, float],
        twap: dict[str, float] | None = None,
        twap_by_window: dict[str, dict[str, float]] | None = None,
    ) -> None:
        async with self._lock:
            self.markets = {market_key(c.asset, c.interval): c for c in cards}
            self.oracle_prices = dict(oracle)
            if twap_by_window is not None:
                self.twap_by_window = {
                    "30": dict(twap_by_window.get("30") or {}),
                    "60": dict(twap_by_window.get("60") or {}),
                }
                self.twap_prices = dict(self.twap_by_window.get("60") or {})
            elif twap is not None:
                self.twap_prices = dict(twap)
                self.twap_by_window = {
                    "30": dict(self.twap_by_window.get("30") or {}),
                    "60": dict(twap),
                }
            self.updated_at = datetime.now(timezone.utc)
            self._oracle_dirty = True
        self._event.set()

    async def clear_history(self, asset: str, interval: str) -> None:
        key = market_key(asset, interval)
        async with self._lock:
            self.history[key] = SecondHistory()
        self._event.set()

    async def load_history(
        self,
        asset: str,
        interval: str,
        points: list[TickPoint],
    ) -> None:
        key = market_key(asset, interval)
        async with self._lock:
            # Merge stored 1s ticks under what live pushes already added: the
            # DB covers the whole window, live pushes the latest seconds.
            buf = self._history_for(key)
            live = buf.points()
            fresh = SecondHistory()
            for point in points:
                fresh.add(point)
            for point in live:
                fresh.add(point)
            self.history[key] = fresh
        self._event.set()

    async def wait_update(self, timeout: float = 2.0) -> None:
        """Wait for the next feed update without dropping a pending event."""
        if self._event.is_set():
            self._event.clear()
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._event.clear()

    def has_pending_update(self) -> bool:
        return self._event.is_set()

    def _feed_payload(self) -> dict:
        return {
            "lead_lag": dict(self.lead_lag),
            "twap_prices": dict(self.twap_prices),
            "twap_by_window": {
                "30": dict(self.twap_by_window.get("30") or {}),
                "60": dict(self.twap_by_window.get("60") or {}),
            },
        }

    async def get_snapshot_payload(self) -> dict:
        async with self._lock:
            return {
                "type": "snapshot",
                "ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
                "oracle_prices": dict(self.oracle_prices),
                "binance_prices": dict(self.binance_prices),
                **self._feed_payload(),
                "markets": [m.model_dump() for m in self.markets.values()],
            }

    async def get_market_card(self, asset: str, interval: str) -> MarketCard | None:
        async with self._lock:
            return self.markets.get(market_key(asset, interval))

    async def enrich_oracle_history(
        self,
        asset: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        oracle_points: list[dict],
        price_at: Callable[[int], float | None] | None = None,
    ) -> None:
        """Backfill oracle_price on history points for the oracle chart."""
        if not oracle_points and not price_at:
            return
        key = market_key(asset, interval)
        async with self._lock:
            buf = self._history_for(key)
            for op in oracle_points:
                if op.get("ts_ms") and op.get("oracle_price"):
                    buf.fill("oracle_price", op["ts_ms"], op["oracle_price"])
            if price_at:
                for point in buf.points():
                    if point.oracle_price is None:
                        point.oracle_price = price_at(point.ts_ms // 1000)
        self._event.set()

    async def enrich_binance_history(
        self,
        asset: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        binance_points: list[dict],
    ) -> None:
        """Backfill binance_price on history points for the price chart."""
        if not binance_points:
            return
        key = market_key(asset, interval)
        async with self._lock:
            buf = self._history_for(key)
            for bp in binance_points:
                if bp.get("ts_ms") and bp.get("binance_price") is not None:
                    buf.fill("binance_price", bp["ts_ms"], bp["binance_price"])
        self._event.set()

    async def enrich_twap_history(
        self,
        asset: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        twap_points: list[dict],
    ) -> None:
        """Backfill Chainlink TWAP on history points for the price chart."""
        if not twap_points:
            return
        key = market_key(asset, interval)
        async with self._lock:
            buf = self._history_for(key)
            ordered = sorted(
                (p["ts_ms"], p["twap_oracle"])
                for p in twap_points
                if p.get("ts_ms") and p.get("twap_oracle") is not None
            )
            for ts_ms, price in ordered:
                buf.fill("twap_oracle", ts_ms, price)
            # Seconds without their own sample carry the nearest prior TWAP.
            if ordered:
                idx, prior = 0, None
                for point in buf.points():
                    while idx < len(ordered) and ordered[idx][0] <= point.ts_ms:
                        prior = ordered[idx][1]
                        idx += 1
                    if point.twap_oracle is None and prior is not None:
                        point.twap_oracle = prior
        self._event.set()

    async def get_history_payload(self, asset: str, interval: str) -> dict:
        key = market_key(asset, interval)
        async with self._lock:
            card = self.markets.get(key)
            buf = self.history.get(key)
            points = buf.points() if buf is not None else []
            if card and card.start_ts and card.end_ts:
                start_ms = card.start_ts * 1000
                end_ms = card.end_ts * 1000
                points = [
                    p
                    for p in points
                    if start_ms - 500 <= p.ts_ms <= end_ms + 500
                ]
        return {
            "type": "history",
            "asset": asset,
            "interval": interval,
            "slug": card.slug if card else "",
            "start_ts": card.start_ts if card else 0,
            "end_ts": card.end_ts if card else 0,
            "strike_price": card.strike_price if card else None,
            "opening_oracle_price": card.opening_oracle_price if card else None,
            "points": [p.to_dict() for p in points],
        }

    async def drain_tick_updates(
        self, since_seq: int, asset: str | None, interval: str | None
    ) -> tuple[list[dict], int]:
        """Queued ticks after since_seq for one market (asset=None: all markets)."""
        async with self._lock:
            updates = [
                payload
                for seq, payload in self._tick_queue
                if seq > since_seq
                and (
                    asset is None
                    or (payload.get("asset") == asset and payload.get("interval") == interval)
                )
            ]
            return updates, self.seq

    async def drain_oracle_update(self) -> dict | None:
        async with self._lock:
            if not self._oracle_dirty:
                return None
            self._oracle_dirty = False
            return {
                "type": "oracle",
                "ts_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
                "oracle_prices": dict(self.oracle_prices),
                "binance_prices": dict(self.binance_prices),
                **self._feed_payload(),
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
