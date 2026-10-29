"""Track active windows in the database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from pmanalysis.candles import seal_window_candle
from pmanalysis.collector.resolution import apply_resolution, windows_to_recheck
from pmanalysis.config import (
    INTERVAL_SECONDS,
    MARKET_SLUGS,
    RESOLUTION_MAX_WAIT_SEC,
    RESOLUTION_RECHECK_HOURS,
    ROLL_PREFETCH_SEC,
)
from pmanalysis.db.features import boundary_twap
from pmanalysis.db.models import Market, Window
from pmanalysis.feeds.chainlink import ChainlinkFeed
from pmanalysis.feeds.gamma import DiscoveredWindow, GammaClient, strike_to_float
from pmanalysis.feeds.slugs import boundary_ts, build_market_slug, start_ts_from_slug
from pmanalysis.collector.window_cache import CachedWindow
from pmanalysis.feeds.opening_price import (
    apply_opening_price,
    capture_final_oracle_price,
    capture_final_twap,
    oracle_from_ticks_at_end,
    seed_opening_from_previous,
)


class WindowManager:
    def __init__(self, gamma: GammaClient) -> None:
        self.gamma = gamma
        self._active_ids: dict[str, int] = {}
        self._next_ids: dict[str, int] = {}

    def load_active_from_db(self, session: Session) -> None:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for asset, interval, _base in MARKET_SLUGS:
            key = f"{asset}:{interval}"
            expected = boundary_ts(now_ts, interval)
            nxt_start = expected + INTERVAL_SECONDS[interval]
            current = (
                session.query(Window)
                .filter_by(asset=asset, interval=interval, start_ts=expected)
                .first()
            )
            nxt = (
                session.query(Window)
                .filter_by(asset=asset, interval=interval, start_ts=nxt_start)
                .first()
            )
            if current is None:
                current = (
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
            if current:
                self._active_ids[key] = current.id
            if nxt:
                self._next_ids[key] = nxt.id

    def get_active_windows(self, session: Session) -> list[Window]:
        if not self._active_ids:
            return []
        ids = list(self._active_ids.values())
        rows = session.query(Window).filter(Window.id.in_(ids)).all()
        by_id = {row.id: row for row in rows}
        return [by_id[i] for i in ids if i in by_id]

    def needs_roll_discovery(self, session: Session, now_ts: int) -> bool:
        for window in self.get_active_windows(session):
            if now_ts >= window.end_ts:
                return True
            expected_start = boundary_ts(now_ts, window.interval)
            if window.start_ts != expected_start:
                return True
        return False

    def _pin_slug_bounds(self, row: Window, interval: str, slug: str | None = None) -> None:
        start = start_ts_from_slug(slug or row.slug)
        if not start:
            return
        row.start_ts = start
        row.end_ts = start + INTERVAL_SECONDS[interval]

    def _merge_discovered(self, row: Window, item: DiscoveredWindow) -> None:
        if item.yes_token_id:
            row.yes_token_id = item.yes_token_id
        if item.no_token_id:
            row.no_token_id = item.no_token_id
        if item.condition_id:
            row.condition_id = item.condition_id
        if item.question:
            row.question = item.question
        self._pin_slug_bounds(row, item.interval, item.slug or row.slug)
        strike = strike_to_float(item.strike_label)
        if strike is not None and row.strike_price is None:
            row.strike_price = strike
            row.strike_source = "question"

    def _upsert_discovered(
        self, session: Session, item: DiscoveredWindow, market: Market
    ) -> Window:
        existing = session.query(Window).filter_by(slug=item.slug).first()
        if existing:
            self._merge_discovered(existing, item)
            session.add(existing)
            return existing
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
        self._pin_slug_bounds(window, item.interval, item.slug)
        session.add(window)
        session.flush()
        return window

    def subscription_token_ids(
        self,
        session: Session,
        now_ts: int | None = None,
        prefetch_sec: int | None = None,
    ) -> list[str]:
        """CLOB tokens for live windows, plus next windows in the last prefetch_sec."""
        now_ts = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
        prefetch_sec = ROLL_PREFETCH_SEC if prefetch_sec is None else prefetch_sec
        ids = list(self._active_ids.values())
        for window in self.get_active_windows(session):
            if window.end_ts - now_ts <= prefetch_sec:
                nxt = self._next_ids.get(f"{window.asset}:{window.interval}")
                if nxt:
                    ids.append(nxt)
        if not ids:
            return []
        out: list[str] = []
        rows = session.query(Window).filter(Window.id.in_(ids)).all()
        for row in rows:
            if row.yes_token_id:
                out.append(row.yes_token_id)
            if row.no_token_id:
                out.append(row.no_token_id)
        return list(dict.fromkeys(out))

    def needs_prefetch(self, session: Session, now_ts: int, prefetch_sec: int) -> bool:
        """True when Gamma should fill a stub or a missing next market."""
        for window in self.get_active_windows(session):
            key = f"{window.asset}:{window.interval}"
            if not window.yes_token_id or not window.no_token_id:
                return True
            nxt_id = self._next_ids.get(key)
            remaining = window.end_ts - now_ts
            if remaining <= prefetch_sec and not nxt_id:
                return True
            if nxt_id:
                nxt = session.query(Window).filter_by(id=nxt_id).first()
                if nxt and (not nxt.yes_token_id or not nxt.no_token_id):
                    return True
        return False

    def roll_to_clock(
        self,
        session: Session,
        now_ts: int,
        chainlink: ChainlinkFeed | None = None,
    ) -> list[tuple[str, str, int | None]]:
        """Point `_active_ids` at the clock bucket. Does not call Gamma."""
        rolled: list[tuple[str, str, int | None]] = []
        market_map = {m.base_slug: m for m in session.query(Market).all()}
        for asset, interval, base_slug in MARKET_SLUGS:
            key = f"{asset}:{interval}"
            expected_start = boundary_ts(now_ts, interval)
            expected_end = expected_start + INTERVAL_SECONDS[interval]
            expected_slug = build_market_slug(asset, interval, expected_start)
            current_id = self._active_ids.get(key)
            current = (
                session.query(Window).filter_by(id=current_id).first() if current_id else None
            )
            if current and current.start_ts == expected_start:
                continue

            prev_id = current_id
            target = None
            next_id = self._next_ids.get(key)
            if next_id:
                nxt = session.query(Window).filter_by(id=next_id).first()
                if nxt and nxt.start_ts == expected_start:
                    target = nxt
            if target is None:
                target = session.query(Window).filter_by(slug=expected_slug).first()
            if target is None:
                market = market_map.get(base_slug)
                if not market:
                    continue
                target = Window(
                    market_id=market.id,
                    slug=expected_slug,
                    asset=asset,
                    interval=interval,
                    start_ts=expected_start,
                    end_ts=expected_end,
                    question="",
                    yes_token_id="",
                    no_token_id="",
                )
                session.add(target)
                session.flush()

            self._active_ids[key] = target.id
            if self._next_ids.get(key) == target.id:
                self._next_ids.pop(key, None)
            if prev_id and prev_id != target.id:
                rolled.append((asset, interval, prev_id))
                prev_row = session.query(Window).filter_by(id=prev_id).first()
                if chainlink and prev_row:
                    capture_final_oracle_price(session, prev_row, chainlink)
                    capture_final_twap(session, prev_row, chainlink)
                    seed_opening_from_previous(session, target, chainlink)
        session.commit()
        return rolled

    def sync_discovered(
        self,
        session: Session,
        discovered: list[DiscoveredWindow],
        chainlink: ChainlinkFeed | None = None,
        now_ts: int | None = None,
    ) -> tuple[list[str], list[tuple[str, str, int | None]]]:
        now_ts = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
        rolled: list[tuple[str, str, int | None]] = []
        market_map = {m.base_slug: m for m in session.query(Market).all()}
        for item in discovered:
            key = f"{item.asset}:{item.interval}"
            market = market_map.get(item.base_slug)
            if not market:
                continue
            window = self._upsert_discovered(session, item, market)
            expected_start = boundary_ts(now_ts, item.interval)
            is_next = item.role == "next" or (
                item.start_ts != 0 and item.start_ts > expected_start
            )
            if is_next:
                if window.start_ts == expected_start:
                    is_next = False
                else:
                    self._next_ids[key] = window.id
                    continue

            if window.start_ts and window.start_ts < expected_start:
                # Stale Gamma current; do not roll the live pointer backward.
                continue

            prev_id = self._active_ids.get(key)
            prev_row = (
                session.query(Window).filter_by(id=prev_id).first() if prev_id else None
            )
            prev_slug = prev_row.slug if prev_row else None
            self._active_ids[key] = window.id
            if self._next_ids.get(key) == window.id:
                self._next_ids.pop(key, None)
            if prev_slug and prev_slug != window.slug:
                rolled.append((item.asset, item.interval, prev_id))
                if chainlink and prev_row:
                    capture_final_oracle_price(session, prev_row, chainlink)
                    capture_final_twap(session, prev_row, chainlink)
                    seed_opening_from_previous(session, window, chainlink)
        session.commit()
        return self.subscription_token_ids(session, now_ts), rolled

    def seed_openings_from_previous(
        self, session: Session, chainlink: ChainlinkFeed
    ) -> None:
        """Fill missing opening prices from the prior window's close."""
        for window in self.get_active_windows(session):
            if window.opening_oracle_price is not None:
                continue
            seed_opening_from_previous(session, window, chainlink)

    async def refresh_opening_prices(
        self, session: Session, chainlink: ChainlinkFeed
    ) -> None:
        self.seed_openings_from_previous(session, chainlink)
        for window in self.get_active_windows(session):
            await apply_opening_price(session, window, chainlink, self.gamma)
        session.commit()

    async def capture_openings_for_asset(
        self, session: Session, asset: str, chainlink: ChainlinkFeed
    ) -> bool:
        """Try to fill missing opening prices for active windows of one asset."""
        changed = False
        for window in self.get_active_windows(session):
            if window.asset != asset:
                continue
            if await apply_opening_price(session, window, chainlink, self.gamma):
                changed = True
        if changed:
            session.commit()
        return changed

    async def refresh_strikes(self, session: Session, chainlink: ChainlinkFeed) -> None:
        for window in self.get_active_windows(session):
            await apply_opening_price(session, window, chainlink, self.gamma)
        session.commit()

    async def finalize_expired(
        self,
        session: Session,
        now_ts: int,
        grace_sec: int,
        chainlink: ChainlinkFeed | None = None,
    ) -> list[Window]:
        """Resolve+finalize windows past end_ts+grace that aren't resolved yet.

        Queries `resolved_at IS NULL` directly instead of iterating
        `_active_ids`: the roll replaces `_active_ids[key]` at the boundary,
        often before this runs. Gamma is retried for up to
        `RESOLUTION_MAX_WAIT_SEC`; after that the window resolves on our own
        TWAP inference (see collector/resolution.py), which
        `recheck_inferred` later reconciles against Gamma.
        """
        finalized: list[Window] = []
        candidates = (
            session.query(Window)
            .filter(Window.resolved_at.is_(None), Window.end_ts + grace_sec <= now_ts)
            .all()
        )
        for window in candidates:
            outcome, _ = await self.gamma.fetch_resolution(window.slug)
            overdue = now_ts - window.end_ts >= RESOLUTION_MAX_WAIT_SEC
            if not outcome and not overdue:
                continue  # Gamma hasn't published a winner yet; retry next sweep.

            if chainlink:
                capture_final_oracle_price(session, window, chainlink)
                capture_final_twap(session, window, chainlink)
            elif window.final_oracle_price is None:
                final = oracle_from_ticks_at_end(session, window.id, window.end_ts)
                if final is not None:
                    window.final_oracle_price = final

            if window.twap_initial is None:
                # Missed the open (e.g. restart): the previous close is the same boundary.
                window.twap_initial = boundary_twap(session, window)
            apply_resolution(window, outcome)
            window.resolved_at = datetime.now(timezone.utc)
            session.add(window)
            seal_window_candle(session, window)
            finalized.append(window)
            key = f"{window.asset}:{window.interval}"
            if self._active_ids.get(key) == window.id:
                del self._active_ids[key]
        session.commit()
        return finalized

    async def recheck_inferred(self, session: Session, now_ts: int) -> list[Window]:
        """Ask Gamma again for windows resolved on inference; flag disagreements."""
        changed: list[Window] = []
        for window in windows_to_recheck(session, now_ts, RESOLUTION_RECHECK_HOURS * 3600):
            outcome, _ = await self.gamma.fetch_resolution(window.slug)
            if not outcome:
                continue
            apply_resolution(window, outcome)
            # Bump resolved_at so dataset fingerprints (max resolved_at) rebuild.
            window.resolved_at = datetime.now(timezone.utc)
            session.add(window)
            changed.append(window)
        if changed:
            session.commit()
        return changed

    def window_duration(self, window: Window | CachedWindow) -> int:
        return INTERVAL_SECONDS.get(window.interval, window.end_ts - window.start_ts)
