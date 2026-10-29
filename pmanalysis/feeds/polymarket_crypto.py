"""Polymarket crypto window prices (Price to Beat / open oracle)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

POLYMARKET_WEB = "https://polymarket.com"
PAST_RESULTS_URL = f"{POLYMARKET_WEB}/api/past-results"

INTERVAL_VARIANT = {
    "5m": "fiveminute",
    "15m": "fifteen",
}


# One AsyncClient per event loop. Building a client loads the CA bundle
# (~150 ms of CPU on the loop) and this runs on every opening-price attempt.
_CLIENTS: dict[int, httpx.AsyncClient] = {}


def _client() -> httpx.AsyncClient:
    loop_id = id(asyncio.get_running_loop())
    client = _CLIENTS.get(loop_id)
    if client is None or client.is_closed:
        # A new loop (e.g. a fresh asyncio.run) cannot use another loop's client.
        _CLIENTS.clear()
        client = _CLIENTS[loop_id] = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    return client


def _iso_utc(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minute_prefix(iso: str) -> str:
    return iso[:16]


async def fetch_price_to_beat(
    asset: str,
    interval: str,
    start_ts: int,
    end_ts: int,
) -> tuple[float | None, str | None]:
    """
    Polymarket's Price to Beat for crypto up/down windows.

    Uses the same past-results feed as polymarket.com (Chainlink open at T0).
    """
    variant = INTERVAL_VARIANT.get(interval)
    if not variant:
        return None, None

    start_iso = _iso_utc(start_ts)
    params = {
        "symbol": asset.upper(),
        "variant": variant,
        "assetType": "crypto",
        "currentEventStartTime": start_iso,
    }

    try:
        response = await _client().get(PAST_RESULTS_URL, params=params)
        if response.status_code != 200:
            log.debug("past-results %s: %s", response.status_code, response.text[:120])
            return None, None
        payload = response.json()
    except Exception as exc:
        log.debug("past-results fetch failed: %s", exc)
        return None, None

    if payload.get("status") != "success":
        return None, None

    results = payload.get("data", {}).get("results", [])
    if not isinstance(results, list):
        return None, None

    start_key = _minute_prefix(start_iso)

    for row in results:
        if not isinstance(row, dict):
            continue
        row_start = str(row.get("startTime", "")).replace(".000Z", "Z")
        if _minute_prefix(row_start) == start_key:
            open_price = row.get("openPrice")
            if open_price is not None:
                return float(open_price), "polymarket_api"

    for row in results:
        if not isinstance(row, dict):
            continue
        row_end = str(row.get("endTime", "")).replace(".000Z", "Z")
        if _minute_prefix(row_end) == start_key:
            close_price = row.get("closePrice")
            if close_price is not None:
                return float(close_price), "polymarket_api"

    return None, None
