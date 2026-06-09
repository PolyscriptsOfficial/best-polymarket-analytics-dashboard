"""Track active windows in the database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from pmanalysis.config import INTERVAL_SECONDS
from pmanalysis.db.models import Market, Window
from pmanalysis.feeds.gamma import DiscoveredWindow, GammaClient, strike_to_float


class WindowManager:
    def __init__(self, gamma: GammaClient) -> None:
        self.gamma = gamma
        self._active_ids: dict[str, int] = {}

    def load_active_from_db(self, session: Session) -> None:
        rows = (
            session.query(Window)
            .filter(Window.outcome.is_(None))
            .order_by(Window.end_ts.desc())
            .all()
        )
        seen: set[str] = set()
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for row in rows:
            key = f"{row.asset}:{row.interval}"
            if key in seen:
                continue
            if row.end_ts > now_ts:
                self._active_ids[key] = row.id
                seen.add(key)

    def get_active_windows(self, session: Session) -> list[Window]:
        if not self._active_ids:
            return []
        ids = list(self._active_ids.values())
        rows = session.query(Window).filter(Window.id.in_(ids)).all()
        by_id = {row.id: row for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def sync_discovered(self, session: Session, discovered: list[DiscoveredWindow]) -> list[str]:
        token_ids: list[str] = []
        market_map = {m.base_slug: m for m in session.query(Market).all()}
        for item in discovered:
            key = f"{item.asset}:{item.interval}"
            market = market_map.get(item.base_slug)
            if not market:
                continue
            existing = session.query(Window).filter_by(slug=item.slug).first()
            if existing:
                self._active_ids[key] = existing.id
            else:
                window = Window(
                    market_id=market.id,
                    slug=item.slug,
                    asset=item.asset,
                    interval=item.interval,
                    condition_id=item.condition_id,
                    yes_token_id=item.yes_token_id,
                    no_token_id=item.no_token_id,
                    question=item.question,
                    start_ts=item.start_ts,
                    end_ts=item.end_ts,
                    strike_price=strike_to_float(item.strike_label),
                    strike_source="question" if item.strike_label != "—" else None,
                )
                session.add(window)
                session.flush()
                self._active_ids[key] = window.id
            token_ids.extend([item.yes_token_id, item.no_token_id])
        session.commit()
        return list(dict.fromkeys(token_ids))

    async def refresh_strikes(self, session: Session) -> None:
        for window in self.get_active_windows(session):
            if window.strike_price is not None and window.strike_source == "gamma":
                continue
            strike, source = await self.gamma.fetch_strike(window.slug)
            if strike is not None:
                window.strike_price = strike
                window.strike_source = source
                session.add(window)
        session.commit()

    async def finalize_expired(self, session: Session, now_ts: int, grace_sec: int) -> list[Window]:
        finalized: list[Window] = []
        for key, window_id in list(self._active_ids.items()):
            window = session.query(Window).filter_by(id=window_id).first()
            if not window or now_ts < window.end_ts + grace_sec:
                continue
            outcome, _ = await self.gamma.fetch_resolution(window.slug)
            if outcome:
                window.outcome = outcome
            window.resolved_at = datetime.now(timezone.utc)
            session.add(window)
            finalized.append(window)
            del self._active_ids[key]
        session.commit()
        return finalized

    def window_duration(self, window: Window) -> int:
        return INTERVAL_SECONDS.get(window.interval, window.end_ts - window.start_ts)
