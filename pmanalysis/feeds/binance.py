"""Binance spot trade WebSocket feed for chart overlay."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import httpx
import websockets

from pmanalysis.config import ASSETS

log = logging.getLogger(__name__)

_SYMBOLS = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
}
_ASSET_FOR_SYMBOL = {v.upper(): k for k, v in _SYMBOLS.items()}


def _combined_stream_url(host: str) -> str:
    streams = "/".join(f"{sym}@trade" for sym in _SYMBOLS.values())
    return f"{host}/stream?streams={streams}"


def parse_trade_msg(raw: str, *, source: str = "binance") -> dict | None:
    """Parse a Binance combined-stream spot trade message."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict) or data.get("e") != "trade":
        return None

    symbol = str(data.get("s", "")).upper()
    asset = _ASSET_FOR_SYMBOL.get(symbol)
    if not asset:
        return None

    try:
        price = float(data.get("p", 0))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    ts_ms = int(data.get("T", 0) or data.get("E", 0) or time.time() * 1000)
    qty = None
    try:
        if data.get("q") not in (None, ""):
            qty = float(data["q"])
    except (TypeError, ValueError):
        qty = None
    trade_id = data.get("t")
    return {
        "asset": asset,
        "price": price,
        "ts_ms": ts_ms,
        "qty": qty,
        "trade_id": str(trade_id) if trade_id is not None else None,
        "source": source,
    }


class BinanceFeed:
    """Binance USDT spot last-trade overlay (not used for settlement)."""

    STREAM_HOST = "wss://stream.binance.com:9443"
    # stream.binance.com returns HTTP 451 from some regions; vision is market-data only.
    STREAM_HOSTS = (
        "wss://data-stream.binance.vision:443",
        "wss://stream.binance.com:9443",
        "wss://stream.binance.com:443",
    )
    REST_TICKER_URLS = (
        "https://data-api.binance.vision/api/v3/ticker/price",
        "https://api.binance.com/api/v3/ticker/price",
    )
    REST_POLL_SEC = 0.5
    SOURCE = "binance"
    PRICE_KEY = "binance_price"
    LOG_NAME = "Binance spot trade feed"

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._history: dict[str, list[tuple[int, float]]] = {a: [] for a in ASSETS}
        self._running = False
        self._ws_connected = False
        self._rest_connected = False
        self._last_event_ts = 0.0
        self._on_tick: Callable[[dict], None] | None = None

    @property
    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    @property
    def connected(self) -> bool:
        return self._ws_connected or self._rest_connected

    def get_price(self, asset: str) -> float | None:
        return self._prices.get(asset)

    def last_event_age_sec(self) -> float | None:
        if not self._last_event_ts:
            return None
        return time.time() - self._last_event_ts

    def history(self, asset: str, max_points: int = 1200) -> list[tuple[int, float]]:
        return self._history.get(asset, [])[-max_points:]

    async def close(self) -> None:
        self._running = False

    def _apply_tick(self, tick: dict) -> None:
        asset = tick["asset"]
        price = tick["price"]
        ts_ms = tick.get("ts_ms") or int(time.time() * 1000)
        self._last_event_ts = time.time()
        self._prices[asset] = price
        hist = self._history.setdefault(asset, [])
        hist.append((ts_ms, price))
        if len(hist) > 1200:
            self._history[asset] = hist[-1200:]

    def _price_at_boundary(
        self,
        hist: list[tuple[int, float]],
        ts_sec: int,
        fallback: float | None = None,
    ) -> float | None:
        """First trade at/after T0, else last trade within 2s before T0."""
        target_ms = ts_sec * 1000
        if not hist:
            return fallback

        for ts_ms, price in hist:
            if ts_ms >= target_ms and ts_ms <= target_ms + 15_000:
                return price

        before = [(ts_ms, price) for ts_ms, price in hist if ts_ms <= target_ms]
        if before:
            closest_ms, closest_price = max(before, key=lambda row: row[0])
            if target_ms - closest_ms <= 2000:
                return closest_price

        return fallback

    def points_for_window(self, asset: str, start_ts: int, end_ts: int) -> list[dict]:
        """Binance trade points inside a window for chart backfill."""
        hist = self._history.get(asset, [])
        if not hist:
            price = self.get_price(asset)
            if price is None:
                return []
            now_ms = int(time.time() * 1000)
            return [{"ts_ms": now_ms, self.PRICE_KEY: price}]

        start_ms = start_ts * 1000
        end_ms = end_ts * 1000
        points: list[dict] = []
        for ts_ms, price in hist:
            if start_ms <= ts_ms <= end_ms:
                points.append({"ts_ms": ts_ms, self.PRICE_KEY: price})

        opening = self._price_at_boundary(hist, start_ts)
        if opening is not None and not any(p["ts_ms"] == start_ms for p in points):
            points.insert(0, {"ts_ms": start_ms, self.PRICE_KEY: opening})

        if not points and hist:
            last_ts, last_price = hist[-1]
            if last_ts >= start_ms - 60_000:
                points.append({
                    "ts_ms": max(last_ts, start_ms),
                    self.PRICE_KEY: last_price,
                })
        return points

    async def _rest_poll_loop(self) -> None:
        """Keep spot last prices flowing when the trade websocket is geo-blocked."""
        urls = tuple(self.REST_TICKER_URLS)
        if not urls:
            return
        symbols = [sym.upper() for sym in _SYMBOLS.values()]
        url_i = 0
        async with httpx.AsyncClient(timeout=8.0) as client:
            while self._running:
                if self._ws_connected:
                    self._rest_connected = False
                    await asyncio.sleep(1.0)
                    continue
                url = urls[url_i % len(urls)]
                got = False
                try:
                    resp = await client.get(url, params={"symbols": json.dumps(symbols)})
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    rows = resp.json()
                    if not isinstance(rows, list):
                        rows = [rows]
                    now_ms = int(time.time() * 1000)
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        asset = _ASSET_FOR_SYMBOL.get(str(row.get("symbol", "")).upper())
                        if not asset:
                            continue
                        try:
                            price = float(row.get("price", 0))
                        except (TypeError, ValueError):
                            continue
                        if price <= 0:
                            continue
                        tick = {
                            "asset": asset,
                            "price": price,
                            "ts_ms": now_ms,
                            "qty": None,
                            "trade_id": None,
                            "source": self.SOURCE,
                        }
                        self._apply_tick(tick)
                        if self._on_tick:
                            self._on_tick(tick)
                        got = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("%s REST poll %s: %s", self.LOG_NAME, url, exc)
                    url_i += 1
                self._rest_connected = got
                await asyncio.sleep(self.REST_POLL_SEC)

    async def run(self, on_tick: Callable[[dict], None] | None = None) -> None:
        self._running = True
        self._on_tick = on_tick
        hosts = list(self.STREAM_HOSTS or (self.STREAM_HOST,))
        host_i = 0
        backoff = 5
        rest_task = asyncio.create_task(self._rest_poll_loop(), name=f"{self.SOURCE}-rest")
        try:
            while self._running:
                host = hosts[host_i % len(hosts)]
                url = _combined_stream_url(host)
                try:
                    async with websockets.connect(
                        url,
                        ping_interval=20,
                        ping_timeout=20,
                        open_timeout=8,
                    ) as ws:
                        self._ws_connected = True
                        backoff = 5
                        log.info("%s connected (%s)", self.LOG_NAME, host)
                        async for raw in ws:
                            if not self._running:
                                break
                            tick = parse_trade_msg(raw, source=self.SOURCE)
                            if not tick:
                                continue
                            self._apply_tick(tick)
                            if self._on_tick:
                                self._on_tick(tick)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._ws_connected = False
                    log.warning("%s disconnected from %s: %s", self.LOG_NAME, host, exc)
                    host_i += 1
                    if host_i % len(hosts) == 0:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60)
                    else:
                        await asyncio.sleep(0.2)
        finally:
            rest_task.cancel()
            try:
                await rest_task
            except asyncio.CancelledError:
                pass
            self._ws_connected = False
            self._rest_connected = False

