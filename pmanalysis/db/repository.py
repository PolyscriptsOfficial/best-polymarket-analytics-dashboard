"""Read-only DB queries for the web layer."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from pmanalysis.config import ASSETS, INTERVALS
from pmanalysis.db.models import Indicator, Tick, Window
from pmanalysis.indicators.compute import win_rate
from pmanalysis.schemas import MarketCard, WindowSummary


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def latest_oracle_prices(session: Session) -> dict[str, float]:
    out: dict[str, float] = {}
    for asset in ASSETS:
        row = (
            session.query(Tick.oracle_price)
            .join(Window, Tick.window_id == Window.id)
            .filter(Window.asset == asset, Tick.oracle_price.isnot(None))
            .order_by(Tick.ts_ms.desc())
            .first()
        )
        if row and row[0] is not None:
            out[asset] = row[0]
    return out


def active_window(session: Session, asset: str, interval: str) -> Window | None:
    now_ts = _now_ts()
    return (
        session.query(Window)
        .filter(
            Window.asset == asset,
            Window.interval == interval,
            Window.outcome.is_(None),
            Window.end_ts > now_ts,
        )
        .order_by(Window.end_ts.asc())
        .first()
    )


def latest_tick(session: Session, window_id: int) -> Tick | None:
    return (
        session.query(Tick)
        .filter_by(window_id=window_id)
        .order_by(Tick.ts_ms.desc())
        .first()
    )


def latest_indicator(session: Session, window_id: int, ts_ms: int) -> Indicator | None:
    return (
        session.query(Indicator)
        .filter_by(window_id=window_id, ts_ms=ts_ms)
        .first()
    )


def build_market_cards(session: Session) -> list[MarketCard]:
    now_ts = _now_ts()
    cards: list[MarketCard] = []
    for asset in ASSETS:
        for interval in INTERVALS:
            window = active_window(session, asset, interval)
            if not window:
                cards.append(_empty_card(asset, interval))
                continue
            tick = latest_tick(session, window.id)
            indicator = latest_indicator(session, window.id, tick.ts_ms) if tick else None
            cards.append(
                MarketCard(
                    asset=asset,
                    interval=interval,
                    slug=window.slug,
                    question=window.question,
                    yes_mid=tick.yes_mid if tick else 0,
                    no_mid=tick.no_mid if tick else 0,
                    yes_ask=tick.yes_ask if tick else 0,
                    no_ask=tick.no_ask if tick else 0,
                    combined_ask=tick.combined_ask if tick else 0,
                    oracle_price=tick.oracle_price if tick else None,
                    strike_price=window.strike_price,
                    dist_from_strike=tick.dist_from_strike if tick else None,
                    secs_remaining=max(0, window.end_ts - now_ts),
                    outcome=window.outcome,
                    arb_edge=indicator.arb_edge if indicator else None,
                    lean=indicator.lean if indicator else None,
                )
            )
    return cards


def _empty_card(asset: str, interval: str) -> MarketCard:
    return MarketCard(
        asset=asset,
        interval=interval,
        slug="",
        question="No active window",
        yes_mid=0,
        no_mid=0,
        yes_ask=0,
        no_ask=0,
        combined_ask=0,
        oracle_price=None,
        strike_price=None,
        dist_from_strike=None,
        secs_remaining=0,
        outcome=None,
        arb_edge=None,
        lean=None,
    )


def list_windows(
    session: Session,
    *,
    limit: int = 100,
    asset: str | None = None,
    interval: str | None = None,
) -> list[WindowSummary]:
    query = session.query(Window).order_by(Window.end_ts.desc())
    if asset:
        query = query.filter(Window.asset == asset.upper())
    if interval:
        query = query.filter(Window.interval == interval)
    rows = query.limit(limit).all()
    return [
        WindowSummary(
            id=row.id,
            slug=row.slug,
            asset=row.asset,
            interval=row.interval,
            start_ts=row.start_ts,
            end_ts=row.end_ts,
            strike_price=row.strike_price,
            outcome=row.outcome,
            archived=row.archived,
            tick_count=session.query(Tick).filter_by(window_id=row.id).count(),
        )
        for row in rows
    ]


def window_ticks_payload(session: Session, slug: str) -> dict | None:
    window = session.query(Window).filter_by(slug=slug).first()
    if not window:
        return None
    ticks = (
        session.query(Tick)
        .filter_by(window_id=window.id)
        .order_by(Tick.ts_ms)
        .all()
    )
    indicators = {
        row.ts_ms: row
        for row in session.query(Indicator).filter_by(window_id=window.id).all()
    }
    return {
        "window": {
            "slug": window.slug,
            "asset": window.asset,
            "interval": window.interval,
            "strike_price": window.strike_price,
            "outcome": window.outcome,
        },
        "ticks": [
            {
                "ts_ms": t.ts_ms,
                "oracle_price": t.oracle_price,
                "yes_mid": t.yes_mid,
                "no_mid": t.no_mid,
                "combined_ask": t.combined_ask,
                "dist_from_strike": t.dist_from_strike,
                "arb_edge": indicators[t.ts_ms].arb_edge if t.ts_ms in indicators else None,
            }
            for t in ticks
        ],
    }


def stats_win_rate(session: Session, asset: str, interval: str) -> float | None:
    rows = (
        session.query(Window.outcome)
        .filter(
            Window.asset == asset.upper(),
            Window.interval == interval,
            Window.outcome.isnot(None),
        )
        .order_by(Window.end_ts.desc())
        .limit(50)
        .all()
    )
    return win_rate([row[0] for row in rows if row[0]])


def db_stats(session: Session) -> dict:
    return {
        "window_count": session.query(Window).count(),
        "tick_count": session.query(Tick).count(),
        "latest_tick": session.query(func.max(Tick.ts_ms)).scalar(),
    }
