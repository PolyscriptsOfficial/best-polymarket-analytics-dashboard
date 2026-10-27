"""Read-only DB queries for the web layer."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from pmanalysis.config import ASSETS, INTERVALS, FEED_RECORD_ENABLED
from pmanalysis.collector.health import FEED_SOURCES, build_feed_row
from pmanalysis.db.models import CollectorGap, Indicator, Tick, Window
from pmanalysis.db.series import load_window_series, preview_points, tick_payload_points
from pmanalysis.schemas import MarketCard, WindowSummary


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def latest_oracle_prices(session: Session) -> dict[str, float]:
    """Latest oracle per asset from 1s ticks; raw oracle_ticks only if feed-record is on."""
    from pmanalysis.db.models import OracleTick

    out: dict[str, float] = {}
    for asset in ASSETS:
        tick_row = (
            session.query(Tick.oracle_price)
            .join(Window, Tick.window_id == Window.id)
            .filter(Window.asset == asset, Tick.oracle_price.isnot(None))
            .order_by(Tick.ts_ms.desc())
            .first()
        )
        if tick_row and tick_row[0] is not None:
            out[asset] = float(tick_row[0])
            continue
        if not FEED_RECORD_ENABLED:
            continue
        row = (
            session.query(OracleTick.price)
            .filter(OracleTick.asset == asset)
            .order_by(OracleTick.ts_ms.desc())
            .first()
        )
        if row and row[0] is not None:
            out[asset] = float(row[0])
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


def opening_oracle_price(session: Session, window: Window) -> float | None:
    if window.opening_oracle_price is not None:
        return window.opening_oracle_price
    from pmanalysis.feeds.opening_price import oracle_from_ticks_at_start

    tick = oracle_from_ticks_at_start(session, window.id, window.start_ts)
    if tick is not None:
        return tick
    if window.strike_price is not None and window.strike_source in (
        "polymarket_api",
        "gamma",
        "chainlink_boundary",
        "chainlink_start",
        "oracle_start",
        "tick_start",
        "prev_window_close",
    ):
        return window.strike_price
    return None


def _dist_from_opening(current: float | None, opening: float | None) -> float | None:
    if current is None or opening is None:
        return None
    return round(current - opening, 2)


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
            opening = opening_oracle_price(session, window)
            oracle = tick.oracle_price if tick else None
            twap = getattr(indicator, "twap_oracle", None) if indicator else None
            # Prefer earliest stored TWAP in this window as Initial Price.
            first_twap = (
                session.query(Indicator.twap_oracle)
                .filter(
                    Indicator.window_id == window.id,
                    Indicator.twap_oracle.isnot(None),
                )
                .order_by(Indicator.ts_ms.asc())
                .first()
            )
            twap_initial = (
                float(first_twap[0]) if first_twap and first_twap[0] is not None else None
            )
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
                    oracle_price=oracle,
                    strike_price=window.strike_price,
                    opening_oracle_price=opening,
                    dist_from_strike=tick.dist_from_strike if tick else None,
                    # Difference = current TWAP − initial TWAP.
                    dist_from_opening=_dist_from_opening(twap, twap_initial),
                    start_ts=window.start_ts,
                    end_ts=window.end_ts,
                    secs_remaining=max(0, window.end_ts - now_ts),
                    outcome=window.outcome,
                    arb_edge=indicator.arb_edge if indicator else None,
                    lean=indicator.lean if indicator else None,
                    twap_oracle=twap,
                    twap_initial=twap_initial,
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
        opening_oracle_price=None,
        dist_from_strike=None,
        dist_from_opening=None,
        start_ts=0,
        end_ts=0,
        secs_remaining=0,
        outcome=None,
        arb_edge=None,
        lean=None,
    )


def list_windows(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    asset: str | None = None,
    interval: str | None = None,
) -> list[WindowSummary]:
    query = session.query(Window).order_by(Window.end_ts.desc())
    if asset:
        query = query.filter(Window.asset == asset.upper())
    if interval:
        query = query.filter(Window.interval == interval)
    if offset:
        query = query.offset(offset)
    if limit and limit > 0:
        query = query.limit(limit)
    rows = query.all()
    if not rows:
        return []

    window_ids = [row.id for row in rows]
    tick_counts = dict(
        session.query(Tick.window_id, func.count(Tick.id))
        .filter(Tick.window_id.in_(window_ids))
        .group_by(Tick.window_id)
        .all()
    )
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
            tick_count=tick_counts.get(row.id, 0),
        )
        for row in rows
    ]


def count_windows(
    session: Session,
    *,
    asset: str | None = None,
    interval: str | None = None,
) -> int:
    query = session.query(func.count(Window.id))
    if asset:
        query = query.filter(Window.asset == asset.upper())
    if interval:
        query = query.filter(Window.interval == interval)
    return int(query.scalar() or 0)


def db_stats(session: Session) -> dict:
    """Fast stats for status page — avoid full-table COUNT on multi-million-row tables."""
    from pmanalysis.db.models import BinanceTrade, ClobQuote, OracleTick

    def _approx_rows(model) -> int:
        # SQLite: MAX(id) is O(index) vs COUNT(*) full scan on huge tables.
        pk = getattr(model, "id", None)
        if pk is None:
            return int(session.query(func.count()).select_from(model).scalar() or 0)
        return int(session.query(func.max(pk)).scalar() or 0)

    # `MAX(Tick.ts_ms)` alone forces a full table scan (~34M rows): the only
    # index on ts_ms is the composite (window_id, ts_ms), so ts_ms isn't the
    # index's leftmost column and SQLite can't do the O(1) rightmost-leaf
    # trick it uses for MAX(id). This alone was responsible for /status and
    # /api/feeds/stats stalling 5-120s. Ticks are written in id order, so the
    # row with the max primary key (fast, id is the actual index) is an
    # accurate proxy for the most recent tick.
    latest_tick = (
        session.query(Tick.ts_ms).order_by(Tick.id.desc()).limit(1).scalar()
    )

    return {
        "window_count": _approx_rows(Window),
        "tick_count": _approx_rows(Tick),
        "binance_trade_count": _approx_rows(BinanceTrade),
        "oracle_tick_count": _approx_rows(OracleTick),
        "clob_quote_count": _approx_rows(ClobQuote),
        "latest_tick": latest_tick,
        "latest_binance_ts": _latest_ts(session, BinanceTrade, "ts_ms"),
        "latest_oracle_ts": _latest_ts(session, OracleTick, "ts_ms"),
        "latest_clob_ts": _latest_ts(session, ClobQuote, "ts_ms"),
    }


def _latest_ts(session: Session, model, ts_attr: str) -> int | None:
    row_id = session.query(func.max(model.id)).scalar()
    if not row_id:
        return None
    return session.query(getattr(model, ts_attr)).filter(model.id == row_id).scalar()


def list_binance_trades(
    session: Session,
    *,
    limit: int = 200,
    offset: int = 0,
    asset: str | None = None,
) -> tuple[list[dict], int]:
    from pmanalysis.db.models import BinanceTrade

    query = session.query(BinanceTrade)
    if asset:
        query = query.filter(BinanceTrade.asset == asset.upper())
    total = query.count()
    rows = (
        query.order_by(BinanceTrade.ts_ms.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "asset": r.asset,
            "ts_ms": r.ts_ms,
            "price": r.price,
            "qty": r.qty,
            "trade_id": r.trade_id,
        }
        for r in rows
    ], total


def list_oracle_ticks(
    session: Session,
    *,
    limit: int = 200,
    offset: int = 0,
    asset: str | None = None,
    source: str | None = None,
) -> tuple[list[dict], int]:
    from pmanalysis.db.models import OracleTick

    query = session.query(OracleTick)
    if asset:
        query = query.filter(OracleTick.asset == asset.upper())
    if source:
        query = query.filter(OracleTick.source == source)
    total = query.count()
    rows = (
        query.order_by(OracleTick.ts_ms.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "asset": r.asset,
            "ts_ms": r.ts_ms,
            "price": r.price,
            "source": r.source,
        }
        for r in rows
    ], total


def list_clob_quotes(
    session: Session,
    *,
    limit: int = 200,
    offset: int = 0,
    asset: str | None = None,
    side: str | None = None,
) -> tuple[list[dict], int]:
    from pmanalysis.db.models import ClobQuote

    query = session.query(ClobQuote)
    if asset:
        query = query.filter(ClobQuote.asset == asset.upper())
    if side:
        query = query.filter(ClobQuote.side == side.upper())
    total = query.count()
    rows = (
        query.order_by(ClobQuote.ts_ms.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "ts_ms": r.ts_ms,
            "token_id": r.token_id,
            "window_id": r.window_id,
            "asset": r.asset,
            "interval": r.interval,
            "side": r.side,
            "best_ask": r.best_ask,
            "best_bid": r.best_bid,
            "event_type": r.event_type,
        }
        for r in rows
    ], total


def window_preview_payload(
    session: Session,
    slug: str,
    *,
    max_points: int = 64,
    window: Window | None = None,
) -> dict | None:
    """Lightweight downsampled YES/NO series for sidebar previews."""
    window = window or session.query(Window).filter_by(slug=slug).first()
    if not window:
        return None
    rows = load_window_series(session, window)
    points = preview_points(rows, max_points)
    return {
        "window": {
            "slug": window.slug,
            "asset": window.asset,
            "interval": window.interval,
            "start_ts": window.start_ts,
            "end_ts": window.end_ts,
            "strike_price": window.strike_price,
            "outcome": window.outcome,
        },
        "tick_count": len(rows),
        "ticks": points,
    }


def list_recent_previews(
    session: Session,
    *,
    asset: str,
    interval: str,
    limit: int = 7,
    page: int = 0,
    skip_current: bool = False,
    max_points: int = 64,
    now_ts: int | None = None,
) -> dict:
    """Paginated recent windows with downsampled chart points.

    The prefetched *next* market (start_ts in the future) is excluded — it has
    no ticks yet and showed up as a blank OPEN card above the live window.
    When skip_current=True, the live/current window is also excluded so the
    sidebar starts at the previous market.
    """
    limit = max(1, min(int(limit), 10))
    page = max(0, int(page))
    base_skip = 1 if skip_current else 0
    offset = base_skip + page * limit
    now_ts = now_ts if now_ts is not None else _now_ts()

    q = (
        session.query(Window)
        .filter(
            Window.asset == asset.upper(),
            Window.interval == interval,
            Window.start_ts <= now_ts,
        )
        .order_by(Window.end_ts.desc())
    )
    # Fetch one extra to know if older pages exist.
    windows = q.offset(offset).limit(limit + 1).all()
    has_older = len(windows) > limit
    windows = windows[:limit]

    items: list[dict] = []
    for window in windows:
        payload = window_preview_payload(
            session, window.slug, max_points=max_points, window=window
        )
        if payload:
            items.append(payload)

    return {
        "items": items,
        "page": page,
        "limit": limit,
        "skip_current": skip_current,
        "has_newer": page > 0,
        "has_older": has_older,
    }


def window_ticks_payload(session: Session, slug: str) -> dict | None:
    window = session.query(Window).filter_by(slug=slug).first()
    if not window:
        return None
    rows = load_window_series(session, window)
    return {
        "window": {
            "slug": window.slug,
            "asset": window.asset,
            "interval": window.interval,
            "start_ts": window.start_ts,
            "end_ts": window.end_ts,
            "strike_price": window.strike_price,
            "opening_oracle_price": opening_oracle_price(session, window),
            "outcome": window.outcome,
        },
        "ticks": tick_payload_points(rows),
    }


def stats_win_rates(session: Session) -> list[dict]:
    """All-time UP/DOWN rates per asset × interval (GROUP BY, not a full scan of ticks)."""
    rows = (
        session.query(Window.asset, Window.interval, Window.outcome, func.count())
        .filter(Window.outcome.in_(("UP", "DOWN")))
        .group_by(Window.asset, Window.interval, Window.outcome)
        .all()
    )
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for asset, interval, outcome, n in rows:
        bucket = counts.setdefault((asset.upper(), interval), {"UP": 0, "DOWN": 0})
        bucket[str(outcome)] = int(n)

    out: list[dict] = []
    for asset in ASSETS:
        for interval in INTERVALS:
            bucket = counts.get((asset, interval), {"UP": 0, "DOWN": 0})
            up, down = bucket["UP"], bucket["DOWN"]
            resolved = up + down
            rate = round(up / resolved * 100, 1) if resolved else None
            out.append(
                {
                    "asset": asset,
                    "interval": interval,
                    "win_rate_up_pct": rate,
                    "resolved": resolved,
                    "up": up,
                    "down": down,
                }
            )
    return out


def stats_for_market(session: Session, asset: str, interval: str) -> dict | None:
    asset = asset.upper()
    if asset not in ASSETS or interval not in INTERVALS:
        return None
    for row in stats_win_rates(session):
        if row["asset"] == asset and row["interval"] == interval:
            return row
    return None


def stats_win_rate(session: Session, asset: str, interval: str) -> float | None:
    row = stats_for_market(session, asset, interval)
    return None if row is None else row["win_rate_up_pct"]


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def serialize_gap(row: CollectorGap) -> dict:
    started = _aware(row.started_at)
    ended = _aware(row.ended_at)
    now = datetime.now(timezone.utc)
    duration = None
    if started is not None:
        duration = round(((ended or now) - started).total_seconds(), 1)
    return {
        "id": row.id,
        "source": row.source,
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "message": row.message or "",
        "duration_sec": duration,
        "open": ended is None,
    }


def list_collector_gaps(
    session: Session,
    *,
    open_only: bool = False,
    limit: int = 20,
) -> list[dict]:
    query = session.query(CollectorGap)
    if open_only:
        rows = (
            query.filter(CollectorGap.ended_at.is_(None))
            .order_by(CollectorGap.started_at.desc())
            .all()
        )
    else:
        rows = query.order_by(CollectorGap.started_at.desc()).limit(max(1, limit)).all()
    return [serialize_gap(row) for row in rows]


def feeds_from_db(session: Session) -> list[dict]:
    """Infer lag from the newest raw-feed rows (split web process / no collector)."""
    stats = db_stats(session)
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    ts_by_source = {
        "binance": stats.get("latest_binance_ts"),
        "chainlink": stats.get("latest_oracle_ts"),
        "clob": stats.get("latest_clob_ts"),
    }
    out: list[dict] = []
    for source in FEED_SOURCES:
        ts_ms = ts_by_source[source]
        lag_sec = None if ts_ms is None else max(0.0, (now_ms - int(ts_ms)) / 1000.0)
        row = build_feed_row(
            source,
            connected=None if ts_ms is None else True,
            lag_sec=lag_sec,
            origin="db",
        )
        out.append(row)
    return out


def collector_status_payload(session: Session) -> dict:
    stats = db_stats(session)
    return {
        **stats,
        "gaps_open": list_collector_gaps(session, open_only=True),
        "gaps_recent": list_collector_gaps(session, open_only=False, limit=12),
    }
