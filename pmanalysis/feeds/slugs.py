"""Polymarket crypto up/down slug helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from pmanalysis.config import INTERVAL_SECONDS


def boundary_ts(now: int | None = None, interval: str = "5m") -> int:
    ts = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    step = INTERVAL_SECONDS[interval]
    return (ts // step) * step


def next_boundary_ts(now: int | None = None, interval: str = "5m") -> int:
    ts = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    return boundary_ts(ts, interval) + INTERVAL_SECONDS[interval]


def start_ts_from_slug(slug: str | None) -> int | None:
    """Clock start encoded in `{asset}-updown-{5m|15m}-{start_ts}`."""
    if not slug:
        return None
    tail = str(slug).rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def build_market_slug(asset: str, interval: str, now: int | None = None) -> str:
    ts = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    base = f"{asset.lower()}-updown-{interval}"
    return f"{base}-{boundary_ts(ts, interval)}"


def market_base_slug(asset: str, interval: str) -> str:
    return f"{asset.lower()}-updown-{interval}"
