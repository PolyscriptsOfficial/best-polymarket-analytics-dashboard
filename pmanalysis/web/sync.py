"""Sync live store from DB when collector runs in a separate process."""

from __future__ import annotations

import asyncio
import logging

from pmanalysis.core.live_store import TickPoint, get_live_store, market_key
from pmanalysis.db.models import Tick, Window
from pmanalysis.db.tick_fields import with_derived
from pmanalysis.db.repository import active_window, build_market_cards, latest_oracle_prices
from pmanalysis.db.session import get_session, run_db

log = logging.getLogger(__name__)

_POINT_STORED = (
    "oracle_price",
    "binance_price",
    "twap_oracle",
    "yes_ask",
    "yes_bid",
    "no_ask",
    "no_bid",
    "fair_yes",
)
_POINT_FIELDS = (
    "ts_ms",
    *_POINT_STORED,
    "yes_mid",
    "no_mid",
    "combined_ask",
    "dist_from_strike",
    "arb_edge",
    "lean",
    "twap_diff",
)

_db_lock = asyncio.Lock()
_history_loaded_slugs: set[str] = set()


def reset_history_load_cache() -> None:
    _history_loaded_slugs.clear()


def invalidate_history_cache(asset: str, interval: str) -> None:
    prefix = f"{market_key(asset, interval)}:"
    # Materialize the matches first: difference_update(genexpr) mutates the
    # same set the generator lazily iterates, which raised "Set changed size
    # during iteration" on every window roll (silently swallowed by the
    # caller's try/except, but it meant this cache never actually got
    # invalidated on the affected roll).
    stale = [k for k in _history_loaded_slugs if k.startswith(prefix)]
    _history_loaded_slugs.difference_update(stale)


def load_history_points(session, window: Window) -> list[TickPoint]:
    """All sampled ticks for a window (YES/NO + oracle) for chart backfill."""
    rows = [
        {"ts_ms": ts_ms, **dict(zip(_POINT_STORED, values))}
        for ts_ms, *values in session.query(
            Tick.ts_ms, *(getattr(Tick, name) for name in _POINT_STORED)
        )
        .filter_by(window_id=window.id)
        .order_by(Tick.ts_ms)
        .all()
    ]
    rows = with_derived(
        rows,
        end_ts=window.end_ts,
        strike_price=window.strike_price,
        twap_initial=window.twap_initial,
    )
    return [TickPoint(**{name: row.get(name) for name in _POINT_FIELDS}) for row in rows]


def _load_history_sync(asset: str, interval: str) -> list[TickPoint]:
    with get_session() as session:
        window = active_window(session, asset, interval)
        if not window:
            return []
        return load_history_points(session, window)


def _hydrate_snapshot_sync() -> tuple[list, dict[str, float]]:
    with get_session() as session:
        return build_market_cards(session), latest_oracle_prices(session)


async def ensure_history_loaded(asset: str, interval: str, *, force: bool = False) -> None:
    """Populate live-store chart history from DB for one market."""
    store = get_live_store()
    key = market_key(asset, interval)
    card = await store.get_market_card(asset, interval)
    slug = card.slug if card else ""
    cache_key = f"{key}:{slug}"
    if not force and cache_key in _history_loaded_slugs:
        buf = store.history.get(key)
        if buf and len(buf) > 3:
            return

    try:
        async with _db_lock:
            if cache_key in _history_loaded_slugs and not force:
                return
            points = await run_db(lambda: _load_history_sync(asset, interval))
            if points:
                await store.load_history(asset, interval, points)
            _history_loaded_slugs.add(cache_key)
    except Exception as exc:
        log.warning("load history for %s %s failed: %s", asset, interval, exc)


async def hydrate_live_store_from_db() -> None:
    """Populate in-memory live store from DB (used at startup and as WS fallback)."""
    store = get_live_store()
    try:
        cards, oracle = await run_db(_hydrate_snapshot_sync)
        await store.set_snapshot(cards, oracle)
        # Tick history is loaded on demand via ensure_history_loaded (keeps startup fast).
    except Exception as exc:
        log.warning("hydrate live store failed: %s", exc)


async def db_sync_loop(interval: float = 1.0) -> None:
    store = get_live_store()
    while True:
        try:
            await hydrate_live_store_from_db()
        except Exception as exc:
            log.warning("db sync failed: %s", exc)
        await asyncio.sleep(interval)
