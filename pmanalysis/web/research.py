"""Research API: calibration, regime stats, rule backtests, resolution audit."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from pmanalysis.analytics.features import checkpoints_for
from pmanalysis.analytics.research import (
    MAX_DAYS,
    BacktestSpec,
    calibration,
    regime_stats,
    run_backtest,
)
from pmanalysis.collector.resolution import mismatch_summary
from pmanalysis.config import ASSETS, INTERVALS
from pmanalysis.db.session import get_session, run_db

router = APIRouter(prefix="/api/research", tags=["research"])


def _market(asset: str, interval: str) -> tuple[str, str]:
    asset = asset.upper()
    if asset not in ASSETS or interval not in INTERVALS:
        raise HTTPException(400, "Invalid asset or interval")
    return asset, interval


def _checkpoint(interval: str, t_minus: int) -> int:
    allowed = checkpoints_for(interval)
    if t_minus not in allowed:
        raise HTTPException(400, f"t_minus must be one of {list(allowed)}")
    return t_minus


def _days(days: int) -> int:
    if not 1 <= days <= MAX_DAYS:
        raise HTTPException(400, f"days must be 1..{MAX_DAYS}")
    return days


@router.get("/calibration")
async def api_calibration(asset: str, interval: str, t_minus: int = 30, days: int = 30):
    asset, interval = _market(asset, interval)
    t_minus = _checkpoint(interval, t_minus)
    days = _days(days)

    def _load():
        with get_session() as session:
            return calibration(
                session, asset=asset, interval=interval, t_minus=t_minus, days=days
            )

    return await run_db(_load)


@router.get("/regimes")
async def api_regimes(asset: str, interval: str, days: int = 30):
    asset, interval = _market(asset, interval)
    days = _days(days)

    def _load():
        with get_session() as session:
            return regime_stats(session, asset=asset, interval=interval, days=days)

    return await run_db(_load)


class BacktestRequest(BaseModel):
    asset: str
    interval: str
    t_minus: int = 30
    days: int = 30
    side: str = Field("AUTO", pattern="^(YES|NO|AUTO)$")
    min_ask: float = Field(1.0, ge=0, le=100)
    max_ask: float = Field(99.0, ge=0, le=100)
    min_diff: float | None = None
    min_edge: float | None = Field(None, ge=-100, le=100)
    shares: float = Field(10.0, gt=0, le=1_000_000)
    require_size: bool = False
    slippage_cents: float = Field(0.0, ge=0, le=50)
    fee_pct: float = Field(0.0, ge=0, le=50)


@router.post("/backtest")
async def api_backtest(body: BacktestRequest):
    asset, interval = _market(body.asset, body.interval)
    spec = BacktestSpec(
        **{
            **body.model_dump(),
            "asset": asset,
            "interval": interval,
            "t_minus": _checkpoint(interval, body.t_minus),
            "days": _days(body.days),
        }
    )
    if spec.min_ask > spec.max_ask:
        raise HTTPException(400, "min_ask must be <= max_ask")

    def _load():
        with get_session() as session:
            return run_backtest(session, spec)

    return await run_db(_load)


@router.get("/resolution")
async def api_resolution(limit: int = 20):
    def _load():
        with get_session() as session:
            return mismatch_summary(session, limit=max(1, min(limit, 200)))

    return await run_db(_load)
