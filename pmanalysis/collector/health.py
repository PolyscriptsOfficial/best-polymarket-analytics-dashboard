"""Feed health classification — used by the collector and the status API."""

from __future__ import annotations

from pmanalysis.config import FEED_LAG_SEC

FEED_SOURCES = ("binance", "chainlink", "clob")
FEED_LABELS = {
    "binance": "Binance spot",
    "chainlink": "Chainlink / RTDS",
    "clob": "CLOB orderbook",
}


def classify_feed(
    *,
    connected: bool | None,
    lag_sec: float | None,
    threshold_sec: float,
    idle: bool = False,
) -> str:
    """Return ok | waiting | stale | down | idle."""
    if idle:
        return "idle"
    if connected is False:
        return "down"
    if lag_sec is None:
        return "waiting"
    if lag_sec >= threshold_sec:
        return "stale"
    return "ok"


def is_gap_status(status: str) -> bool:
    return status in ("down", "stale")


def build_feed_row(
    source: str,
    *,
    connected: bool | None,
    lag_sec: float | None,
    idle: bool = False,
    origin: str = "live",
) -> dict:
    threshold = float(FEED_LAG_SEC[source])
    status = classify_feed(
        connected=connected,
        lag_sec=lag_sec,
        threshold_sec=threshold,
        idle=idle,
    )
    lag_out = None if lag_sec is None else round(float(lag_sec), 1)
    return {
        "source": source,
        "label": FEED_LABELS[source],
        "connected": connected,
        "lag_sec": lag_out,
        "threshold_sec": threshold,
        "status": status,
        "gap": is_gap_status(status),
        "origin": origin,
    }
