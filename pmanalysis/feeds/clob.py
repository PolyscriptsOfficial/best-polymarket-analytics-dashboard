"""CLOB REST and WebSocket helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import httpx
import websockets

from pmanalysis.config import BOOK_DEPTH_BAND_CENTS, CLOB_API, CLOB_WS_URL

log = logging.getLogger(__name__)


def valid_ask_cents(value: float) -> bool:
    return 0 < value < 98


def book_to_cents(book: dict) -> tuple[float, float]:
    asks = book.get("asks", []) if isinstance(book, dict) else []
    bids = book.get("bids", []) if isinstance(book, dict) else []
    best_ask = round(min(float(a["price"]) for a in asks) * 100, 1) if asks else 0.0
    best_bid = round(max(float(b["price"]) for b in bids) * 100, 1) if bids else 0.0
    return best_ask, best_bid


def _price_to_cents(raw: str | float | None) -> float:
    if raw in (None, "", "0"):
        return 0.0
    try:
        return round(float(raw) * 100, 1)
    except (TypeError, ValueError):
        return 0.0


def _quote_update(
    token_id: str,
    best_ask: float,
    best_bid: float,
    *,
    event_type: str | None = None,
    ts_ms: int | None = None,
) -> dict:
    row = {
        "token_id": str(token_id),
        "best_ask": best_ask,
        "best_bid": best_bid,
        "event_type": event_type,
    }
    if ts_ms is not None:
        row["ts_ms"] = ts_ms
    return row


def parse_clob_ws(msg) -> list[dict]:
    """Parse market-channel messages into normalized quote updates."""
    out: list[dict] = []

    if isinstance(msg, list):
        for item in msg:
            if not isinstance(item, dict):
                continue
            token_id = item.get("asset_id", "")
            if not token_id:
                continue
            best_ask, best_bid = book_to_cents(item)
            out.append(_quote_update(token_id, best_ask, best_bid, event_type="book"))
        return out

    if not isinstance(msg, dict):
        return out

    event_type = msg.get("event_type")
    ts_ms = None
    for key in ("timestamp", "ts", "t"):
        raw_ts = msg.get(key)
        if raw_ts is None:
            continue
        try:
            ts_ms = int(raw_ts)
            if ts_ms < 1_000_000_000_000:
                ts_ms *= 1000
            break
        except (TypeError, ValueError):
            pass

    if event_type == "best_bid_ask":
        token_id = str(msg.get("asset_id", ""))
        if token_id:
            out.append(
                _quote_update(
                    token_id,
                    _price_to_cents(msg.get("best_ask")),
                    _price_to_cents(msg.get("best_bid")),
                    event_type=event_type,
                    ts_ms=ts_ms,
                )
            )
        return out

    if event_type == "book":
        token_id = str(msg.get("asset_id", ""))
        if token_id:
            best_ask, best_bid = book_to_cents(msg)
            out.append(
                _quote_update(
                    token_id, best_ask, best_bid, event_type=event_type, ts_ms=ts_ms
                )
            )
        return out

    if event_type == "price_change":
        for change in msg.get("price_changes", []):
            token_id = str(change.get("asset_id", ""))
            if not token_id:
                continue
            out.append(
                _quote_update(
                    token_id,
                    _price_to_cents(change.get("best_ask")),
                    _price_to_cents(change.get("best_bid")),
                    event_type=event_type,
                    ts_ms=ts_ms,
                )
            )
        return out

    # Legacy / snapshot shapes without event_type
    if msg.get("asset_id") and (msg.get("asks") is not None or msg.get("bids") is not None):
        token_id = str(msg.get("asset_id", ""))
        if token_id:
            best_ask, best_bid = book_to_cents(msg)
            out.append(
                _quote_update(
                    token_id, best_ask, best_bid, event_type="book", ts_ms=ts_ms
                )
            )

    for change in msg.get("price_changes", []):
        token_id = str(change.get("asset_id", ""))
        if not token_id:
            continue
        out.append(
            _quote_update(
                token_id,
                _price_to_cents(change.get("best_ask")),
                _price_to_cents(change.get("best_bid")),
                event_type="price_change",
                ts_ms=ts_ms,
            )
        )

    return out


def _levels(raw_levels) -> dict[float, float]:
    out: dict[float, float] = {}
    for level in raw_levels or []:
        try:
            price = round(float(level["price"]) * 100, 1)
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if size > 0:
            out[price] = size
    return out


class TokenBook:
    """Aggregated price levels for one outcome token (prices in cents, sizes in shares)."""

    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def replace(self, book: dict) -> None:
        self.bids = _levels(book.get("bids"))
        self.asks = _levels(book.get("asks"))

    def set_level(self, side: str, price_cents: float, size: float) -> None:
        levels = self.bids if side == "BUY" else self.asks
        if size > 0:
            levels[price_cents] = size
        else:
            levels.pop(price_cents, None)

    def stats(self, best_ask: float, best_bid: float, band: float) -> dict:
        """Size at the best quote and depth within ``band`` cents of it."""
        ask_size = self.asks.get(best_ask) if best_ask > 0 else None
        bid_size = self.bids.get(best_bid) if best_bid > 0 else None
        ask_depth = (
            round(sum(s for p, s in self.asks.items() if p <= best_ask + band), 2)
            if best_ask > 0 and self.asks
            else None
        )
        bid_depth = (
            round(sum(s for p, s in self.bids.items() if p >= best_bid - band), 2)
            if best_bid > 0 and self.bids
            else None
        )
        return {
            "ask_size": ask_size,
            "bid_size": bid_size,
            "ask_depth": ask_depth,
            "bid_depth": bid_depth,
        }


class TradeTape:
    """Last trade price and cumulative traded notional for one token."""

    __slots__ = ("last_price", "notional")

    def __init__(self) -> None:
        self.last_price: float | None = None
        self.notional = 0.0

    def add(self, price_cents: float, size: float) -> None:
        self.last_price = price_cents
        self.notional += price_cents / 100.0 * size


class ClobFeed:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        self._prices: dict[str, dict[str, float]] = {}
        self._last_update: dict[str, float] = {}
        self._books: dict[str, TokenBook] = {}
        self._trades: dict[str, TradeTape] = {}
        self._running = False
        self._connected = False

    @property
    def prices(self) -> dict[str, dict[str, float]]:
        return self._prices

    @property
    def connected(self) -> bool:
        return self._connected

    def last_event_age_sec(self) -> float | None:
        if not self._last_update:
            return None
        return time.monotonic() - max(self._last_update.values())

    async def close(self) -> None:
        self._running = False
        await self._client.aclose()

    async def fetch_book(self, token_id: str) -> dict:
        response = await self._client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        data = response.json()
        best_ask, best_bid = book_to_cents(data)
        if isinstance(data, dict):
            self._book_for(token_id).replace(data)
        return {"best_ask": best_ask, "best_bid": best_bid, "raw": data}

    def _book_for(self, token_id: str) -> TokenBook:
        book = self._books.get(token_id)
        if book is None:
            book = self._books[token_id] = TokenBook()
        return book

    def apply_book_events(self, msg) -> None:
        """Maintain per-token depth and trade tapes from market-channel events.

        ``book`` replaces a token's levels, ``price_change`` sets one level's
        aggregate size (0 removes it), ``last_trade_price`` records a fill.
        """
        items = msg if isinstance(msg, list) else [msg]
        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type")
            if event_type == "book" or (
                event_type is None and item.get("asset_id") and "asks" in item
            ):
                token_id = str(item.get("asset_id", ""))
                if token_id:
                    self._book_for(token_id).replace(item)
            elif event_type == "price_change":
                for change in item.get("price_changes", []):
                    token_id = str(change.get("asset_id", ""))
                    side = str(change.get("side", "")).upper()
                    try:
                        price = round(float(change["price"]) * 100, 1)
                        size = float(change["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if token_id and side in ("BUY", "SELL"):
                        self._book_for(token_id).set_level(side, price, size)
            elif event_type == "last_trade_price":
                token_id = str(item.get("asset_id", ""))
                try:
                    price = round(float(item["price"]) * 100, 1)
                    size = float(item["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                if token_id and size > 0:
                    tape = self._trades.get(token_id)
                    if tape is None:
                        tape = self._trades[token_id] = TradeTape()
                    tape.add(price, size)

    def book_stats(self, token_id: str) -> dict:
        """Best-level size and banded depth for a token (None when unknown)."""
        best_ask, best_bid = self.get_quote(token_id)
        book = self._books.get(token_id)
        if book is None:
            return {"ask_size": None, "bid_size": None, "ask_depth": None, "bid_depth": None}
        return book.stats(best_ask, best_bid, BOOK_DEPTH_BAND_CENTS)

    def last_trade(self, token_id: str) -> float | None:
        tape = self._trades.get(token_id)
        return tape.last_price if tape else None

    def traded_notional(self, token_id: str) -> float:
        """Cumulative USDC traded on this token since subscription."""
        tape = self._trades.get(token_id)
        return tape.notional if tape else 0.0

    def apply_quote(self, token_id: str, best_ask: float, best_bid: float) -> bool:
        """Merge partial quote updates; return True if visible prices changed."""
        self._last_update[token_id] = time.monotonic()
        prev = self._prices.get(token_id, {})
        ask = best_ask if best_ask > 0 else prev.get("best_ask", 0.0)
        bid = best_bid if best_bid > 0 else prev.get("best_bid", 0.0)
        if ask == prev.get("best_ask", 0.0) and bid == prev.get("best_bid", 0.0):
            return False
        self._prices[token_id] = {"best_ask": ask, "best_bid": bid}
        return True

    def quote_age_sec(self, token_id: str) -> float | None:
        """Seconds since this token's quote was last confirmed (WS or REST)."""
        last = self._last_update.get(token_id)
        return None if last is None else time.monotonic() - last

    def stale_tokens(self, token_ids: list[str], max_age_sec: float) -> list[str]:
        """Token ids with no WS quote (or none at all) within max_age_sec.

        Lets the REST /book poll act as a fallback for a dead/slow WS feed
        instead of a fixed-rate poll racing the WebSocket reader.
        """
        now = time.monotonic()
        out = []
        for token_id in token_ids:
            last = self._last_update.get(token_id)
            if last is None or (now - last) >= max_age_sec:
                out.append(token_id)
        return out

    async def seed_tokens(self, token_ids: list[str]) -> list[str]:
        """Refresh books from REST; return token ids whose quotes changed."""

        async def _one(token_id: str) -> str | None:
            try:
                book = await self.fetch_book(token_id)
                changed = self.apply_quote(token_id, book["best_ask"], book["best_bid"])
                self._last_update[token_id] = time.monotonic()
                if changed:
                    return token_id
            except Exception as exc:
                log.debug("seed book failed for %s: %s", token_id, exc)
            return None

        results = await asyncio.gather(*[_one(t) for t in token_ids])
        return [t for t in results if t]

    def subscribe_msg(self, token_ids: list[str]) -> str:
        return json.dumps({
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        })

    def dynamic_subscribe_msg(self, token_ids: list[str]) -> str:
        return json.dumps({
            "assets_ids": token_ids,
            "operation": "subscribe",
            "custom_feature_enabled": True,
        })

    def unsubscribe_msg(self, token_ids: list[str]) -> str:
        return json.dumps({
            "assets_ids": token_ids,
            "operation": "unsubscribe",
        })

    async def _align_subscriptions(
        self,
        ws,
        subscribed: set[str],
        get_token_ids: Callable[[], list[str]],
    ) -> None:
        wanted = set(get_token_ids())
        missing = wanted - subscribed
        extra = subscribed - wanted
        if missing:
            await ws.send(self.dynamic_subscribe_msg(list(missing)))
            subscribed.update(missing)
        if extra:
            await ws.send(self.unsubscribe_msg(list(extra)))
            subscribed.difference_update(extra)

    async def _ping_loop(self, ws) -> None:
        while self._running:
            try:
                await ws.send("PING")
            except Exception:
                break
            await asyncio.sleep(10)

    async def run(
        self,
        get_token_ids: Callable[[], list[str]],
        on_update: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._running = True
        backoff = 5
        while self._running:
            token_ids = list(dict.fromkeys(get_token_ids()))
            if not token_ids:
                await asyncio.sleep(2)
                continue
            subscribed: set[str] = set()
            try:
                async with websockets.connect(CLOB_WS_URL, ping_interval=None) as ws:
                    await ws.send(self.subscribe_msg(token_ids))
                    subscribed = set(token_ids)
                    backoff = 5
                    self._connected = True
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            if raw == "PONG":
                                await self._align_subscriptions(ws, subscribed, get_token_ids)
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            self.apply_book_events(payload)
                            updates = parse_clob_ws(payload)
                            changed: list[dict] = []
                            for upd in updates:
                                token_id = upd["token_id"]
                                if self.apply_quote(
                                    token_id, upd["best_ask"], upd["best_bid"]
                                ):
                                    quote = self._prices.get(token_id, {})
                                    changed.append({
                                        "token_id": token_id,
                                        "best_ask": quote.get("best_ask", 0.0),
                                        "best_bid": quote.get("best_bid", 0.0),
                                        "event_type": upd.get("event_type"),
                                        "ts_ms": upd.get("ts_ms"),
                                    })

                            await self._align_subscriptions(ws, subscribed, get_token_ids)

                            if changed and on_update:
                                on_update(changed)
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                log.warning("CLOB WS disconnected: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def get_quote(self, token_id: str) -> tuple[float, float]:
        quote = self._prices.get(token_id, {})
        return quote.get("best_ask", 0.0), quote.get("best_bid", 0.0)

    def prune_quotes(self, keep_token_ids: set[str] | list[str]) -> int:
        """Drop cached quotes for tokens we no longer subscribe to (bounded memory)."""
        keep = set(keep_token_ids)
        removed = 0
        for token_id in list(self._prices.keys()):
            if token_id not in keep:
                self._prices.pop(token_id, None)
                self._last_update.pop(token_id, None)
                removed += 1
        for cache in (self._books, self._trades):
            for token_id in [t for t in cache if t not in keep]:
                cache.pop(token_id, None)
        return removed
