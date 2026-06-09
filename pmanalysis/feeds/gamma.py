"""Gamma API client for market discovery and resolution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from pmanalysis.config import (
    DISCOVERY_LOOKAHEAD_MINUTES,
    GAMMA_API,
    INTERVAL_SECONDS,
    MARKET_SLUGS,
)

log = logging.getLogger(__name__)
_STRIKE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")


def parse_strike(question: str) -> str:
    match = _STRIKE_RE.search(question or "")
    return match.group(0) if match else "—"


def strike_to_float(strike: str | float | None) -> float | None:
    if strike is None:
        return None
    if isinstance(strike, (int, float)):
        return float(strike)
    cleaned = str(strike).replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned == "—":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def boundary_ts(now: int | None = None, interval: str = "5m") -> int:
    ts = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    step = INTERVAL_SECONDS[interval]
    return (ts // step) * step


@dataclass
class DiscoveredWindow:
    asset: str
    interval: str
    base_slug: str
    slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    question: str
    strike_label: str
    start_ts: int
    end_ts: int
    secs_left: int


class GammaClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def discover_active_windows(self) -> list[DiscoveredWindow]:
        """Discover live windows by direct slug fetch (reliable) + bulk scan fallback."""
        out: list[DiscoveredWindow] = []
        seen_slugs: set[str] = set()

        async def fetch_direct(asset: str, interval: str, base_slug: str) -> DiscoveredWindow | None:
            slug = f"{base_slug}-{boundary_ts(interval=interval)}"
            market = await self.fetch_market_by_slug(slug)
            if not market or not market.get("active"):
                return None
            return self._parse_market(asset, interval, base_slug, market)

        direct = await asyncio.gather(
            *[fetch_direct(asset, interval, base) for asset, interval, base in MARKET_SLUGS],
            return_exceptions=True,
        )
        for result in direct:
            if isinstance(result, DiscoveredWindow) and result.slug not in seen_slugs:
                out.append(result)
                seen_slugs.add(result.slug)

        if not out:
            out.extend(await self._discover_bulk())

        log.info("Discovered %d active windows", len(out))
        return out

    async def _discover_bulk(self) -> list[DiscoveredWindow]:
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=DISCOVERY_LOOKAHEAD_MINUTES)
        try:
            response = await self._client.get(
                f"{GAMMA_API}/markets",
                params={
                    "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_date_max": soon.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 200,
                },
                headers={"Cache-Control": "no-cache"},
            )
            raw = response.json()
        except Exception as exc:
            log.warning("bulk discovery failed: %s", exc)
            return []

        markets = raw if isinstance(raw, list) else []
        slug_to_market: dict[str, dict] = {}
        for market in markets:
            if not market.get("active"):
                continue
            slug = market.get("slug", "")
            for _, _, base_slug in MARKET_SLUGS:
                if slug.startswith(base_slug) and base_slug not in slug_to_market:
                    slug_to_market[base_slug] = market

        out: list[DiscoveredWindow] = []
        for asset, interval, base_slug in MARKET_SLUGS:
            market = slug_to_market.get(base_slug)
            if not market:
                continue
            parsed = self._parse_market(asset, interval, base_slug, market)
            if parsed:
                out.append(parsed)
        return out

    def _parse_market(
        self, asset: str, interval: str, base_slug: str, market: dict
    ) -> DiscoveredWindow | None:
        tokens = market.get("clobTokenIds", [])
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                tokens = []
        if len(tokens) < 2:
            return None

        end_ts = 0
        secs_left = 0
        try:
            end_dt = datetime.fromisoformat(market.get("endDate", "").replace("Z", "+00:00"))
            end_ts = int(end_dt.timestamp())
            secs_left = max(0, int((end_dt - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            pass

        if end_ts <= int(datetime.now(timezone.utc).timestamp()):
            return None

        start_ts = 0
        try:
            est = market.get("eventStartTime", "")
            if est:
                start_ts = int(
                    datetime.fromisoformat(est.replace("Z", "+00:00")).timestamp()
                )
        except (TypeError, ValueError):
            start_ts = end_ts - INTERVAL_SECONDS[interval] if end_ts else 0

        return DiscoveredWindow(
            asset=asset,
            interval=interval,
            base_slug=base_slug,
            slug=market.get("slug", f"{base_slug}-{boundary_ts(interval=interval)}"),
            condition_id=market.get("conditionId", "") or "",
            yes_token_id=str(tokens[0]),
            no_token_id=str(tokens[1]),
            question=market.get("question", ""),
            strike_label=parse_strike(market.get("question", "")),
            start_ts=start_ts,
            end_ts=end_ts,
            secs_left=secs_left,
        )

    async def fetch_strike(self, slug: str) -> tuple[float | None, str | None]:
        response = await self._client.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
        )
        data = response.json()
        events = data if isinstance(data, list) else []
        if not events:
            return None, None
        event = events[0]
        meta = event.get("eventMetadata") or {}
        price = meta.get("priceToBeat")
        if price is None:
            return None, None
        try:
            return float(price), "gamma"
        except (TypeError, ValueError):
            return None, None

    async def fetch_resolution(self, slug: str) -> tuple[str | None, float | None]:
        response = await self._client.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "limit": 1},
        )
        data = response.json()
        if not isinstance(data, list) or not data:
            return None, None
        market = data[0]
        try:
            prices = json.loads(market.get("outcomePrices", "[]"))
            if len(prices) >= 2:
                up = float(prices[0])
                down = float(prices[1])
                if up > 0.9:
                    return "UP", up
                if down > 0.9:
                    return "DOWN", down
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return None, None

    async def fetch_market_by_slug(self, slug: str) -> dict | None:
        response = await self._client.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "limit": 1},
        )
        data = response.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
