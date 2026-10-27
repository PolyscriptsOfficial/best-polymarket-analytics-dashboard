"""Gamma API client for market discovery and resolution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from pmanalysis.config import DISCOVERY_LOOKAHEAD_MINUTES, GAMMA_API, INTERVAL_SECONDS, MARKET_SLUGS
from pmanalysis.feeds.slugs import boundary_ts, build_market_slug, start_ts_from_slug

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
    role: str = "current"


class GammaClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def discover_active_windows(self) -> list[DiscoveredWindow]:
        """Fetch the clock-current slug and the next slug for CLOB overlap.

        Gamma `active` is not required: the next market is often created before
        it is marked active. Clock roll must not wait on that flag.
        """
        out: list[DiscoveredWindow] = []
        seen_slugs: set[str] = set()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        async def fetch_direct(
            asset: str, interval: str, base_slug: str, when_ts: int, role: str
        ) -> DiscoveredWindow | None:
            slug = build_market_slug(asset, interval, when_ts)
            market = await self.fetch_market_by_slug(slug)
            if not market:
                return None
            parsed = self._parse_market(asset, interval, base_slug, market)
            if parsed:
                parsed.role = role
                start = boundary_ts(when_ts, interval)
                parsed.start_ts = start
                parsed.end_ts = start + INTERVAL_SECONDS[interval]
            return parsed

        jobs = []
        for asset, interval, base in MARKET_SLUGS:
            jobs.append(fetch_direct(asset, interval, base, now_ts, "current"))
            jobs.append(
                fetch_direct(
                    asset, interval, base, now_ts + INTERVAL_SECONDS[interval], "next"
                )
            )

        direct = await asyncio.gather(*jobs, return_exceptions=True)
        for result in direct:
            if isinstance(result, BaseException):
                log.warning(
                    "direct discovery failed: %r",
                    result,
                    exc_info=result,
                )
                continue
            if isinstance(result, DiscoveredWindow) and result.slug not in seen_slugs:
                out.append(result)
                seen_slugs.add(result.slug)

        if not any(item.role == "current" for item in out):
            for item in await self._discover_bulk():
                if item.slug not in seen_slugs:
                    item.role = "current"
                    out.append(item)
                    seen_slugs.add(item.slug)

        log.info(
            "Discovered %d windows (%d current, %d next)",
            len(out),
            sum(1 for item in out if item.role == "current"),
            sum(1 for item in out if item.role == "next"),
        )
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
            log.warning("bulk discovery failed: %r", exc, exc_info=True)
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

        now_ts = int(datetime.now(timezone.utc).timestamp())
        slug = market.get("slug") or build_market_slug(asset, interval)
        start_ts = start_ts_from_slug(slug) or 0
        end_ts = start_ts + INTERVAL_SECONDS[interval] if start_ts else 0
        if not start_ts:
            try:
                est = market.get("eventStartTime", "")
                if est:
                    start_ts = int(
                        datetime.fromisoformat(est.replace("Z", "+00:00")).timestamp()
                    )
            except (TypeError, ValueError):
                start_ts = 0
            try:
                end_dt = datetime.fromisoformat(
                    market.get("endDate", "").replace("Z", "+00:00")
                )
                end_ts = int(end_dt.timestamp())
            except (TypeError, ValueError):
                end_ts = 0
            if start_ts and not end_ts:
                end_ts = start_ts + INTERVAL_SECONDS[interval]
            elif end_ts and not start_ts:
                start_ts = end_ts - INTERVAL_SECONDS[interval]

        if end_ts and end_ts <= now_ts:
            return None

        return DiscoveredWindow(
            asset=asset,
            interval=interval,
            base_slug=base_slug,
            slug=slug,
            condition_id=market.get("conditionId", "") or "",
            yes_token_id=str(tokens[0]),
            no_token_id=str(tokens[1]),
            question=market.get("question", ""),
            strike_label=parse_strike(market.get("question", "")),
            start_ts=start_ts,
            end_ts=end_ts,
            secs_left=max(0, end_ts - now_ts) if end_ts else 0,
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

        for meta in (event.get("eventMetadata"), event):
            price = self._parse_price_to_beat(meta)
            if price is not None:
                return price, "gamma"

        event_id = event.get("id")
        if event_id:
            try:
                detail = await self._client.get(f"{GAMMA_API}/events/{event_id}")
                if detail.status_code == 200:
                    detail_event = detail.json()
                    for meta in (detail_event.get("eventMetadata"), detail_event):
                        price = self._parse_price_to_beat(meta)
                        if price is not None:
                            return price, "gamma"
            except Exception:
                pass

        return None, None

    def _parse_price_to_beat(self, meta: object) -> float | None:
        if not isinstance(meta, dict):
            return None
        price = meta.get("priceToBeat")
        if price is None:
            return None
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

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
