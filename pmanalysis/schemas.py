"""Shared Pydantic models (used by collector, core, db, and web)."""

from __future__ import annotations

from pydantic import BaseModel


class MarketCard(BaseModel):
    asset: str
    interval: str
    slug: str
    question: str
    yes_mid: float
    no_mid: float
    yes_ask: float
    no_ask: float
    combined_ask: float
    oracle_price: float | None
    strike_price: float | None
    dist_from_strike: float | None
    secs_remaining: int
    outcome: str | None
    arb_edge: float | None
    lean: float | None


class Snapshot(BaseModel):
    ts_ms: int
    markets: list[MarketCard]
    oracle_prices: dict[str, float]


class WindowSummary(BaseModel):
    id: int
    slug: str
    asset: str
    interval: str
    start_ts: int
    end_ts: int
    strike_price: float | None
    outcome: str | None
    archived: bool
    tick_count: int
