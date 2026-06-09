"""CLOB REST and WebSocket helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import httpx
import websockets

from pmanalysis.config import CLOB_API, CLOB_WS_URL

log = logging.getLogger(__name__)


def book_to_cents(book: dict) -> tuple[float, float]:
    asks = book.get("asks", []) if isinstance(book, dict) else []
    bids = book.get("bids", []) if isinstance(book, dict) else []
    best_ask = round(min(float(a["price"]) for a in asks) * 100, 1) if asks else 0.0
    best_bid = round(max(float(b["price"]) for b in bids) * 100, 1) if bids else 0.0
    return best_ask, best_bid


def valid_ask_cents(value: float) -> bool:
    return 0 < value < 98


def parse_clob_ws(msg) -> list[dict]:
    out: list[dict] = []
    if isinstance(msg, list):
        for item in msg:
            token_id = item.get("asset_id", "")
            if not token_id:
                continue
            best_ask, best_bid = book_to_cents(item)
            out.append({"token_id": token_id, "best_ask": best_ask, "best_bid": best_bid})
    elif isinstance(msg, dict):
        if msg.get("asset_id") and (msg.get("asks") is not None or msg.get("bids") is not None):
            token_id = msg.get("asset_id", "")
            if token_id:
                best_ask, best_bid = book_to_cents(msg)
                out.append({"token_id": token_id, "best_ask": best_ask, "best_bid": best_bid})
        for change in msg.get("price_changes", []):
            token_id = change.get("asset_id", "")
            ask_raw = change.get("best_ask")
            bid_raw = change.get("best_bid")
            if not token_id or (ask_raw is None and bid_raw is None):
                continue
            best_ask = round(float(ask_raw) * 100, 1) if ask_raw not in (None, "", "0") else 0.0
            best_bid = round(float(bid_raw) * 100, 1) if bid_raw not in (None, "", "0") else 0.0
            out.append({"token_id": token_id, "best_ask": best_ask, "best_bid": best_bid})
    return out


class ClobFeed:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        self._prices: dict[str, dict[str, float]] = {}
        self._running = False

    @property
    def prices(self) -> dict[str, dict[str, float]]:
        return self._prices

    async def close(self) -> None:
        self._running = False
        await self._client.aclose()

    async def fetch_book(self, token_id: str) -> dict:
        response = await self._client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        data = response.json()
        best_ask, best_bid = book_to_cents(data)
        return {"best_ask": best_ask, "best_bid": best_bid, "raw": data}

    async def seed_tokens(self, token_ids: list[str]) -> None:
        async def _one(token_id: str) -> None:
            try:
                book = await self.fetch_book(token_id)
                self._prices[token_id] = {
                    "best_ask": book["best_ask"],
                    "best_bid": book["best_bid"],
                }
            except Exception as exc:
                log.debug("seed book failed for %s: %s", token_id, exc)

        await asyncio.gather(*[_one(t) for t in token_ids])

    async def run(
        self,
        token_ids: list[str],
        on_update: Callable[[list[dict]], None] | None = None,
    ) -> None:
        self._running = True
        backoff = 5
        while self._running:
            try:
                async with websockets.connect(CLOB_WS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
                    backoff = 5
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            updates = parse_clob_ws(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
                        if not updates:
                            continue
                        for upd in updates:
                            token_id = upd["token_id"]
                            self._prices[token_id] = {
                                "best_ask": upd["best_ask"],
                                "best_bid": upd["best_bid"],
                            }
                        if on_update:
                            on_update(updates)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("CLOB WS disconnected: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def get_quote(self, token_id: str) -> tuple[float, float]:
        quote = self._prices.get(token_id, {})
        return quote.get("best_ask", 0.0), quote.get("best_bid", 0.0)
