"""WebSocket live stream handler."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from pmanalysis.core.live_store import get_live_store
from pmanalysis.config import ASSETS, INTERVALS

log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._selection: dict[WebSocket, tuple[str, str]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        self._selection[ws] = ("BTC", "5m")

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        self._selection.pop(ws, None)

    async def send_json(self, ws: WebSocket, payload: dict) -> None:
        await ws.send_text(json.dumps(payload))

    async def handle_client(self, ws: WebSocket) -> None:
        store = get_live_store()
        await self.connect(ws)
        try:
            await self.send_json(ws, await store.get_snapshot_payload())
            asset, interval = self._selection[ws]
            await self.send_json(ws, await store.get_history_payload(asset, interval))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=0.05)
                    msg = json.loads(raw)
                    if msg.get("type") == "select":
                        asset = str(msg.get("asset", "BTC")).upper()
                        interval = str(msg.get("interval", "5m"))
                        if asset in ASSETS and interval in INTERVALS:
                            self._selection[ws] = (asset, interval)
                            await self.send_json(
                                ws, await store.get_history_payload(asset, interval)
                            )
                except asyncio.TimeoutError:
                    pass
                except json.JSONDecodeError:
                    pass

                await store.wait_update(timeout=2.0)
                await self.send_json(ws, await store.get_snapshot_payload())
                asset, interval = self._selection[ws]
                history = await store.get_history_payload(asset, interval)
                if history.get("points"):
                    last = history["points"][-1]
                    card = await store.get_market_card(asset, interval)
                    if card:
                        await self.send_json(
                            ws,
                            {
                                "type": "tick",
                                "asset": asset,
                                "interval": interval,
                                "market": card.model_dump(),
                                "point": last,
                            },
                        )
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("ws client error: %s", exc)
        finally:
            self.disconnect(ws)


manager = ConnectionManager()
