"""Edge / arbitrage / feed-gap alerts: browser (via LiveStore), Discord, Telegram."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from pmanalysis import config
from pmanalysis.collector.snapshot import MarketState

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str  # "edge" | "arb" | "feed_gap"
    key: str  # cooldown key, e.g. "edge:BTC:5m:YES"
    title: str
    message: str
    asset: str | None = None
    interval: str | None = None
    slug: str | None = None
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        return {
            "type": "alert",
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "asset": self.asset,
            "interval": self.interval,
            "slug": self.slug,
            "ts_ms": self.ts_ms,
        }


def evaluate_market(state: MarketState) -> list[Alert]:
    """Pure rule check for one market sample (thresholds from config)."""
    out: list[Alert] = []
    w = state.window
    label = f"{w.asset} {w.interval}"
    if state.secs_remaining < config.ALERT_MIN_SECS_LEFT or not state.fresh:
        # A frozen book or spot yields phantom edges; feed-gap alerts cover outages.
        return out

    for side, edge, ask in (("YES", state.edge_yes, state.yes_ask), ("NO", state.edge_no, state.no_ask)):
        if edge is not None and edge >= config.ALERT_EDGE_CENTS:
            out.append(
                Alert(
                    kind="edge",
                    key=f"edge:{w.slug}:{side}",
                    title=f"{label} {side} edge +{edge:.1f}¢",
                    message=(
                        f"Fair YES {state.fair_yes:.1f}¢ vs {side} ask {ask:.1f}¢ · "
                        f"TWAP diff {state.twap_diff if state.twap_diff is not None else '—'} · "
                        f"{state.secs_remaining}s left"
                    ),
                    asset=w.asset,
                    interval=w.interval,
                    slug=w.slug,
                )
            )

    if state.yes_ask > 0 and state.no_ask > 0:
        combined = state.yes_ask + state.no_ask
        if combined <= 100 - config.ALERT_ARB_CENTS:
            out.append(
                Alert(
                    kind="arb",
                    key=f"arb:{w.slug}",
                    title=f"{label} YES+NO = {combined:.1f}¢",
                    message=(
                        f"YES {state.yes_ask:.1f}¢ + NO {state.no_ask:.1f}¢ locks "
                        f"{100 - combined:.1f}¢/share before fees · {state.secs_remaining}s left"
                    ),
                    asset=w.asset,
                    interval=w.interval,
                    slug=w.slug,
                )
            )
    return out


def feed_gap_alert(source: str, message: str) -> Alert:
    return Alert(
        kind="feed_gap",
        key=f"gap:{source}",
        title=f"Feed gap: {source}",
        message=message,
    )


class AlertEngine:
    """Applies cooldowns and fans alerts out without blocking the caller."""

    def __init__(self, publish) -> None:
        self._publish = publish  # async callable(dict) -> None (LiveStore.push_alert)
        self._last_fired: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def external_enabled(self) -> bool:
        return bool(
            config.ALERT_DISCORD_WEBHOOK
            or (config.ALERT_TELEGRAM_TOKEN and config.ALERT_TELEGRAM_CHAT_ID)
        )

    def _cooled(self, key: str, now: float) -> bool:
        last = self._last_fired.get(key)
        if last is not None and now - last < config.ALERT_COOLDOWN_SEC:
            return False
        self._last_fired[key] = now
        if len(self._last_fired) > 2000:
            cutoff = now - config.ALERT_COOLDOWN_SEC
            self._last_fired = {k: t for k, t in self._last_fired.items() if t >= cutoff}
        return True

    async def emit(self, alerts: list[Alert]) -> None:
        now = time.monotonic()
        for alert in alerts:
            if not self._cooled(alert.key, now):
                continue
            await self._publish(alert.to_dict())
            if self.external_enabled:
                task = asyncio.create_task(self._send_external(alert), name="alert-send")
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _send_external(self, alert: Alert) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        text = f"{alert.title}\n{alert.message}"
        if alert.slug:
            text += f"\nhttps://polymarket.com/event/{alert.slug}"
        if config.ALERT_DISCORD_WEBHOOK:
            await self._post("discord", config.ALERT_DISCORD_WEBHOOK, {"content": text})
        if config.ALERT_TELEGRAM_TOKEN and config.ALERT_TELEGRAM_CHAT_ID:
            await self._post(
                "telegram",
                f"https://api.telegram.org/bot{config.ALERT_TELEGRAM_TOKEN}/sendMessage",
                {
                    "chat_id": config.ALERT_TELEGRAM_CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )

    async def _post(self, channel: str, url: str, payload: dict) -> None:
        # Each channel fails independently. Never log the URL or exception text:
        # both embed the webhook secret / bot token.
        try:
            response = await self._client.post(url, json=payload)
        except Exception as exc:
            log.warning("%s alert delivery failed: %s", channel, type(exc).__name__)
            return
        if response.status_code >= 400:
            log.warning("%s alert rejected: HTTP %s", channel, response.status_code)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
