"""Sync live store from DB when collector runs in a separate process."""

from __future__ import annotations

import asyncio
import logging

from pmanalysis.core.live_store import TickPoint, get_live_store
from pmanalysis.db.repository import build_market_cards, latest_oracle_prices
from pmanalysis.db.session import get_session

log = logging.getLogger(__name__)


async def db_sync_loop(interval: float = 1.0) -> None:
    store = get_live_store()
    while True:
        try:
            with get_session() as session:
                cards = build_market_cards(session)
                oracle = latest_oracle_prices(session)
                await store.set_snapshot(cards, oracle)
                for card in cards:
                    if not card.slug:
                        continue
                    from pmanalysis.db.models import Tick, Window
                    from pmanalysis.db.repository import latest_indicator

                    window = (
                        session.query(Window)
                        .filter_by(slug=card.slug)
                        .first()
                    )
                    if not window:
                        continue
                    ticks = (
                        session.query(Tick)
                        .filter_by(window_id=window.id)
                        .order_by(Tick.ts_ms.desc())
                        .limit(720)
                        .all()
                    )
                    ticks.reverse()
                    points = []
                    for t in ticks:
                        ind = latest_indicator(session, window.id, t.ts_ms)
                        points.append(
                            TickPoint(
                                ts_ms=t.ts_ms,
                                oracle_price=t.oracle_price,
                                yes_mid=t.yes_mid,
                                no_mid=t.no_mid,
                                combined_ask=t.combined_ask,
                                dist_from_strike=t.dist_from_strike,
                                arb_edge=ind.arb_edge if ind else None,
                                lean=ind.lean if ind else None,
                            )
                        )
                    await store.load_history(card.asset, card.interval, points)
        except Exception as exc:
            log.warning("db sync failed: %s", exc)
        await asyncio.sleep(interval)
