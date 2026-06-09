"""Polymarket RTDS Chainlink oracle feed."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

from pmanalysis.config import RTDS_URL

log = logging.getLogger(__name__)

_TOPIC = "crypto_prices_chainlink"
_SYMBOL_ASSET = {
    "btc/usd": "BTC",
    "eth/usd": "ETH",
    "sol/usd": "SOL",
}


def subscribe_msg() -> str:
    return json.dumps({
        "action": "subscribe",
        "subscriptions": [{"topic": _TOPIC, "type": "*"}],
    })


def parse_msg(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if payload.get("topic") != _TOPIC:
        return None
    body = payload.get("payload") or {}
    symbol = str(body.get("symbol", "")).lower()
    asset = _SYMBOL_ASSET.get(symbol)
    if not asset:
        return None
    try:
        price = float(body.get("value"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {"asset": asset, "price": price, "ts_ms": int(body.get("timestamp", 0))}


class ChainlinkFeed:
    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._history: dict[str, list[tuple[int, float]]] = {a: [] for a in _SYMBOL_ASSET.values()}
        self._running = False

    @property
    def prices(self) -> dict[str, float]:
        return self._prices

    def history(self, asset: str, max_points: int = 300) -> list[tuple[int, float]]:
        return self._history.get(asset, [])[-max_points:]

    async def close(self) -> None:
        self._running = False

    async def run(self, on_tick: Callable[[dict], None] | None = None) -> None:
        self._running = True
        backoff = 5
        while self._running:
            try:
                async with websockets.connect(RTDS_URL, ping_interval=20) as ws:
                    await ws.send(subscribe_msg())
                    backoff = 5
                    async for raw in ws:
                        if not self._running:
                            break
                        tick = parse_msg(raw)
                        if not tick:
                            continue
                        asset = tick["asset"]
                        price = tick["price"]
                        ts_ms = tick.get("ts_ms") or int(asyncio.get_event_loop().time() * 1000)
                        self._prices[asset] = price
                        hist = self._history.setdefault(asset, [])
                        hist.append((ts_ms, price))
                        if len(hist) > 600:
                            self._history[asset] = hist[-600:]
                        if on_tick:
                            on_tick(tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Chainlink WS disconnected: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def get_price(self, asset: str) -> float | None:
        return self._prices.get(asset)
