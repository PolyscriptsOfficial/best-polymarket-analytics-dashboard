"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "pmanalysis.db"
ARCHIVE_DIR = DATA_DIR / "archive"
EXPORT_DIR = DATA_DIR / "exports"

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_URL = "wss://ws-live-data.polymarket.com"

ASSETS = ("BTC", "ETH", "SOL")
INTERVALS = ("5m", "15m")

INTERVAL_SECONDS = {"5m": 300, "15m": 900}

MARKET_SLUGS: list[tuple[str, str, str]] = [
    (asset, interval, f"{asset.lower()}-updown-{interval}")
    for asset in ASSETS
    for interval in INTERVALS
]

DISCOVERY_LOOKAHEAD_MINUTES = 70
STRIKE_POLL_INTERVAL_SEC = 10
WINDOW_ROLL_GRACE_SEC = 15
COLLECTOR_TICK_INTERVAL_SEC = 1.0
DISCOVERY_INTERVAL_SEC = 30

WEB_HOST = os.getenv("PM_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("PM_PORT", "8001"))


@dataclass(frozen=True)
class MarketKey:
    asset: str
    interval: str
    base_slug: str

    @property
    def step_seconds(self) -> int:
        return INTERVAL_SECONDS[self.interval]


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
