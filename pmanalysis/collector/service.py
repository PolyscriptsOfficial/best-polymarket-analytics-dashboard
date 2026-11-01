"""Always-on collector orchestrator."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from pmanalysis.analytics.live import LiveAnalytics
from pmanalysis.candles import CandleBook, hydrate_book, persist_candles
from pmanalysis.collector.feed_recorder import FeedRecorder
from pmanalysis.collector.gaps import GapTracker
from pmanalysis.collector.alerts import AlertEngine, evaluate_market, feed_gap_alert
from pmanalysis.collector.health import build_feed_row
from pmanalysis.collector.sampler import TickSampler
from pmanalysis.collector.snapshot import SnapshotBuilder
from pmanalysis.collector.window_cache import CachedWindow
from pmanalysis.collector.window_manager import WindowManager
from pmanalysis.collector.worker import CoalescingWorker, DbWriteWorker
from pmanalysis.config import (
    BOOK_POLL_INTERVAL_SEC,
    CLOB_STALE_SEC,
    COLLECTOR_TICK_INTERVAL_SEC,
    DISCOVERY_INTERVAL_SEC,
    FEED_RECORD_ENABLED,
    FEED_HEALTH_INTERVAL_SEC,
    LEAD_LAG_REFRESH_SEC,
    LIVE_PUSH_MIN_INTERVAL_MS,
    OPENING_CAPTURE_DEBOUNCE_SEC,
    QUOTE_PUSH_INTERVAL_SEC,
    RESOLUTION_RECHECK_INTERVAL_SEC,
    RETENTION_SWEEP_INTERVAL_SEC,
    ROLL_PREFETCH_SEC,
    ROLL_WATCH_INTERVAL_SEC,
    STRIKE_POLL_INTERVAL_SEC,
    TWAP_WINDOW_SEC,
    WINDOW_ROLL_GRACE_SEC,
)
from pmanalysis.core.live_store import LiveStore, get_live_store
from pmanalysis.db.features import compute_window_features
from pmanalysis.db.session import get_session, init_db
from pmanalysis.export.parquet import export_window_parquet
from pmanalysis.feeds.binance import BinanceFeed
from pmanalysis.feeds.chainlink import ChainlinkFeed
from pmanalysis.feeds.clob import ClobFeed
from pmanalysis.feeds.gamma import GammaClient

log = logging.getLogger(__name__)


class CollectorService:
    def __init__(self, live_store: LiveStore | None = None) -> None:
        self.live_store = live_store or get_live_store()
        self.gamma = GammaClient()
        self.chainlink = ChainlinkFeed()
        self.binance = BinanceFeed()
        self.clob = ClobFeed()
        self.window_manager = WindowManager(self.gamma)
        self.candles = CandleBook()
        self.analytics = LiveAnalytics()
        self.builder = SnapshotBuilder(
            self.chainlink, self.clob, self.analytics, binance=self.binance
        )
        self.sampler = TickSampler(self.window_manager, self.builder)
        self.alerts = AlertEngine(self.live_store.push_alert)
        self._tasks: list[asyncio.Task] = []
        self._token_ids: list[str] = []
        self._token_to_window_id: dict[str, int] = {}
        self._active_window_ids: list[int] = []
        self._window_ids_by_asset: dict[str, list[int]] = {}
        self._last_live_push_ms: dict[str, int] = {}
        self._window_cache: dict[int, CachedWindow] = {}
        self._pending_push_ids: set[int] = set()
        self._push_coalesce_task: asyncio.Task | None = None
        self._pending_oracle_prices: dict[str, float] = {}
        self._pending_binance_prices: dict[str, float] = {}
        self._twap_dirty = False
        self._price_flush_task: asyncio.Task | None = None
        self._db_worker = DbWriteWorker("collector-db")
        self._feed_recorder = FeedRecorder(self._db_worker)
        self._gap_tracker = GapTracker()
        self._feed_snapshot: list[dict] = []
        self._opening_worker = CoalescingWorker(
            "opening-capture",
            self._capture_openings_for_asset,
            debounce_sec=OPENING_CAPTURE_DEBOUNCE_SEC,
            db_worker=self._db_worker,
        )
        self._running = False
        self.last_tick_at: datetime | None = None
        self.last_discovery_at: datetime | None = None
        # Refreshed off the hot path so /status and /api/feeds/stats never wait
        # behind the SQLite writer (those endpoints used to stall 5-120s under
        # write load since they queried the DB directly on every request).
        self.stats_cache: dict | None = None

    async def start(self) -> None:
        init_db()
        with get_session() as session:
            self.window_manager.load_active_from_db(session)
            self._refresh_token_map(session)
            hydrate_book(session, self.candles, self.window_manager.get_active_windows(session))

        self._db_worker.start()
        if FEED_RECORD_ENABLED:
            self._feed_recorder.start()
        self._running = True
        with get_session() as session:
            self._gap_tracker.hydrate(session)
        await self._discover_once()
        self._tasks = [
            asyncio.create_task(
                self.chainlink.run(on_tick=self._on_oracle_tick), name="chainlink"
            ),
            asyncio.create_task(
                self.binance.run(on_tick=self._on_binance_tick), name="binance"
            ),
            asyncio.create_task(self._oracle_broadcast_loop(), name="oracle-broadcast"),
            asyncio.create_task(self._clob_loop(), name="clob"),
            asyncio.create_task(self._quote_push_loop(), name="quote-push"),
            asyncio.create_task(self._book_poll_loop(), name="book-poll"),
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._strike_loop(), name="strike"),
            asyncio.create_task(self._sampler_loop(), name="sampler"),
            asyncio.create_task(self._finalize_loop(), name="finalize"),
            asyncio.create_task(self._roll_watch_loop(), name="roll-watch"),
            asyncio.create_task(self._retention_loop(), name="retention"),
            asyncio.create_task(self._stats_cache_loop(), name="stats-cache"),
            asyncio.create_task(self._feed_health_loop(), name="feed-health"),
            asyncio.create_task(self._lead_lag_loop(), name="lead-lag"),
            asyncio.create_task(self._resolution_recheck_loop(), name="resolution-recheck"),
        ]
        log.info(
            "Collector started with %d windows, %d token subscriptions (feed_record=%s)",
            len(self._active_window_ids),
            len(self._token_ids),
            FEED_RECORD_ENABLED,
        )
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._opening_worker.drain()
        if FEED_RECORD_ENABLED:
            await self._feed_recorder.stop()
            log.info("Feed recorder totals: %s", self._feed_recorder.counts)
        await self._db_worker.stop()
        await self.alerts.close()
        await self.gamma.close()
        await self.clob.close()
        await self.chainlink.close()
        await self.binance.close()

    def _refresh_window_cache(self, session) -> None:
        self._window_cache = {
            window.id: CachedWindow.from_row(window)
            for window in self.window_manager.get_active_windows(session)
        }

    def _refresh_token_map(self, session) -> None:
        self._token_to_window_id = {}
        self._active_window_ids = []
        self._window_ids_by_asset = {}
        for window in self.window_manager.get_active_windows(session):
            self._active_window_ids.append(window.id)
            self._window_ids_by_asset.setdefault(window.asset, []).append(window.id)
            if window.yes_token_id:
                self._token_to_window_id[window.yes_token_id] = window.id
            if window.no_token_id:
                self._token_to_window_id[window.no_token_id] = window.id
        self._refresh_window_cache(session)

    async def _discover_once(self) -> None:
        discovered = await self.gamma.discover_active_windows()
        self._token_ids, rolled = await self._db_worker.run(
            "discover-sync",
            lambda: self._persist_discovery_sync(discovered),
        )
        # Opening-price network refresh off the DB worker (may await HTTP).
        await self._refresh_openings_after_discover()
        for asset, interval, prev_window_id in rolled:
            await self.live_store.clear_history(asset, interval)
            self._last_live_push_ms.pop(f"{asset}:{interval}", None)
            self.sampler.drop_state(prev_window_id)
            from pmanalysis.web.sync import invalidate_history_cache

            invalidate_history_cache(asset, interval)
        self.last_discovery_at = datetime.now(timezone.utc)
        self.clob.prune_quotes(self._token_ids)
        await self._push_all_active_windows()
        if self._token_ids:
            changed = await self.clob.seed_tokens(self._token_ids)
            try:
                await self._push_windows_for_tokens(changed)
            except Exception as exc:
                log.warning("initial live push failed: %r", exc, exc_info=True)

    def _persist_discovery_sync(
        self, discovered: list
    ) -> tuple[list[str], list[tuple[str, str, int | None]]]:
        with get_session() as session:
            token_ids, rolled = self.window_manager.sync_discovered(
                session, discovered, self.chainlink
            )
            self._refresh_token_map(session)
            self.window_manager.seed_openings_from_previous(session, self.chainlink)
            self._refresh_window_cache(session)
            self._token_ids = token_ids
        return token_ids, rolled

    def _roll_to_clock_sync(self, now_ts: int) -> list[tuple[str, str, int | None]]:
        with get_session() as session:
            rolled = self.window_manager.roll_to_clock(session, now_ts, self.chainlink)
            if rolled:
                self._refresh_token_map(session)
                self._token_ids = self.window_manager.subscription_token_ids(session, now_ts)
                self._refresh_window_cache(session)
            return rolled

    def _need_prefetch_sync(self) -> bool:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        with get_session() as session:
            return self.window_manager.needs_prefetch(session, now_ts, ROLL_PREFETCH_SEC)

    def _refresh_token_ids_sync(self, now_ts: int) -> list[str]:
        with get_session() as session:
            tokens = self.window_manager.subscription_token_ids(session, now_ts)
            self._token_ids = tokens
            return tokens

    async def _refresh_openings_after_discover(self) -> None:
        try:
            await self._run_strike_poll()
        except Exception as exc:
            log.warning("post-discover opening refresh failed: %r", exc, exc_info=True)

    async def _roll_watch_loop(self) -> None:
        """Swap to the clock bucket immediately; Gamma fills tokens in the background."""
        last_prefetch = asyncio.get_running_loop().time()
        while self._running:
            try:
                now_ts = int(datetime.now(timezone.utc).timestamp())
                prev_tokens = list(self._token_ids)
                rolled = await self._db_worker.run(
                    "clock-roll",
                    lambda: self._roll_to_clock_sync(now_ts),
                )
                tokens = await self._db_worker.run(
                    "sub-tokens",
                    lambda: self._refresh_token_ids_sync(now_ts),
                )
                if rolled:
                    for asset, interval, prev_window_id in rolled:
                        await self.live_store.clear_history(asset, interval)
                        self._last_live_push_ms.pop(f"{asset}:{interval}", None)
                        self.sampler.drop_state(prev_window_id)
                        from pmanalysis.web.sync import invalidate_history_cache

                        invalidate_history_cache(asset, interval)
                    await self._push_all_active_windows()
                    last_prefetch = 0.0
                if tokens != prev_tokens or rolled:
                    if tokens:
                        self.clob.prune_quotes(tokens)
                        try:
                            await self.clob.seed_tokens(tokens)
                        except Exception as exc:
                            log.warning("CLOB seed failed: %r", exc)
                now_mono = asyncio.get_running_loop().time()
                if now_mono - last_prefetch >= 5:
                    need = await self._db_worker.run(
                        "need-prefetch", self._need_prefetch_sync
                    )
                    if need:
                        last_prefetch = now_mono
                        await self._discover_once()
            except Exception as exc:
                log.warning("roll watch failed: %r", exc, exc_info=True)
            await asyncio.sleep(ROLL_WATCH_INTERVAL_SEC)

    async def _push_all_active_windows(self) -> None:
        for window_id in list(self._active_window_ids):
            cached = self._window_cache.get(window_id)
            if cached:
                self._last_live_push_ms.pop(f"{cached.asset}:{cached.interval}", None)
            await self._push_live_for_window_id(window_id)

    async def _discovery_loop(self) -> None:
        while self._running:
            await asyncio.sleep(DISCOVERY_INTERVAL_SEC)
            if not self._running:
                break
            try:
                await self._discover_once()
            except Exception as exc:
                log.warning("discovery failed: %r", exc, exc_info=True)

    def _needs_roll_discovery_sync(self, now_ts: int) -> bool:
        with get_session() as session:
            return self.window_manager.needs_roll_discovery(session, now_ts)

    async def _strike_loop(self) -> None:
        while self._running:
            try:
                changed_ids = await self._run_strike_poll()
                for window_id in changed_ids:
                    cached = self._window_cache.get(window_id)
                    if cached:
                        key = f"{cached.asset}:{cached.interval}"
                        self._last_live_push_ms.pop(key, None)
                    await self._push_live_for_window_id(window_id)
            except Exception as exc:
                log.warning("strike poll failed: %r", exc, exc_info=True)
            await asyncio.sleep(STRIKE_POLL_INTERVAL_SEC)

    async def _run_strike_poll(self) -> list[int]:
        """Refresh strikes; DB snapshots run in the worker thread."""
        before = await self._db_worker.run("strike-snapshot", self._strike_snapshot_sync)
        # Network + short DB writes per window (yields between windows).
        changed_ids: list[int] = []
        for window_id, prev_opening in before:
            changed = await self._refresh_one_strike(window_id)
            if changed:
                changed_ids.append(window_id)
            await asyncio.sleep(0)
        await self._db_worker.run("strike-cache", self._refresh_window_cache_sync)
        return changed_ids

    def _strike_snapshot_sync(self) -> list[tuple[int, float | None]]:
        with get_session() as session:
            return [
                (w.id, w.opening_oracle_price)
                for w in self.window_manager.get_active_windows(session)
            ]

    def _refresh_window_cache_sync(self) -> None:
        with get_session() as session:
            self._refresh_window_cache(session)

    async def _refresh_one_strike(self, window_id: int) -> bool:
        from pmanalysis.db.models import Window
        from pmanalysis.feeds.opening_price import apply_opening_price

        with get_session() as session:
            window = session.query(Window).filter_by(id=window_id).first()
            if not window:
                return False
            before = window.opening_oracle_price
            await apply_opening_price(session, window, self.chainlink, self.gamma)
            return window.opening_oracle_price is not None and before is None

    async def _sampler_loop(self) -> None:
        """Sample on whole-second boundaries so every tick's ts_ms is exact."""
        step = COLLECTOR_TICK_INTERVAL_SEC
        while self._running:
            await asyncio.sleep(step - (time.time() % step) + 0.005)
            now_sec = int(time.time())
            try:
                samples = await self._db_worker.run(
                    "sampler", lambda: self._sample_sync(now_sec)
                )
                alerts = []
                for sample in samples:
                    await self.live_store.push_tick(
                        sample.asset, sample.interval, sample.card, sample.point
                    )
                    alerts.extend(evaluate_market(sample.state))
                if samples:
                    self.last_tick_at = datetime.now(timezone.utc)
                if alerts:
                    await self.alerts.emit(alerts)
            except Exception as exc:
                log.warning("sampler failed: %r", exc, exc_info=True)

    def _sample_sync(self, now_sec: int):
        with get_session() as session:
            samples = self.sampler.sample_all(session, now_sec)
        dirty = self.candles.take_dirty()
        if dirty:
            with get_session() as session:
                persist_candles(session, dirty)
        return samples

    async def _finalize_loop(self) -> None:
        while self._running:
            try:
                finalized = await self._run_finalize()
                if finalized:
                    await self._discover_once()
            except Exception as exc:
                log.warning("finalize failed: %r", exc, exc_info=True)
            await asyncio.sleep(5)

    async def _retention_loop(self) -> None:
        """Keep SQLite bounded: drop hot ticks after parquet archive.

        Runs off the serialized DB worker queue so 30-minute sweeps cannot stall
        the 1 Hz sampler (which shares that queue for writes).
        """
        from pmanalysis.db.cleanup import run_retention

        await asyncio.sleep(120)
        while self._running:
            await asyncio.sleep(RETENTION_SWEEP_INTERVAL_SEC)
            if not self._running:
                break
            try:
                await asyncio.to_thread(run_retention)
            except Exception as exc:
                log.warning("retention sweep failed: %r", exc, exc_info=True)

    def snapshot_feeds(self) -> list[dict]:
        """In-memory feed health for /status (does not touch SQLite)."""
        clob_idle = not self._token_ids
        return [
            build_feed_row(
                "binance",
                connected=self.binance.connected,
                lag_sec=self.binance.last_event_age_sec(),
            ),
            build_feed_row(
                "chainlink",
                connected=self.chainlink.connected,
                lag_sec=self.chainlink.last_event_age_sec(),
            ),
            build_feed_row(
                "clob",
                connected=False if clob_idle else self.clob.connected,
                lag_sec=None if clob_idle else self.clob.last_event_age_sec(),
                idle=clob_idle,
            ),
        ]

    async def _feed_health_loop(self) -> None:
        """Classify feed lag and persist CollectorGap open/close transitions."""
        await asyncio.sleep(3)
        while self._running:
            try:
                snapshot = self.snapshot_feeds()
                self._feed_snapshot = snapshot

                def _persist():
                    with get_session() as session:
                        return self._gap_tracker.reconcile(session, snapshot)

                opened = await self._db_worker.run("feed-gaps", _persist)
                if opened:
                    await self.alerts.emit(
                        [feed_gap_alert(source, message) for source, message in opened]
                    )
            except Exception as exc:
                log.warning("feed health loop failed: %r", exc, exc_info=True)
            await asyncio.sleep(FEED_HEALTH_INTERVAL_SEC)

    async def _stats_cache_loop(self) -> None:
        """Compute /status + /api/feeds/stats data on the DB worker, on a fixed
        cadence, so HTTP requests read a plain dict instead of querying SQLite
        (and stalling behind the collector's writes) on every page load."""
        from pmanalysis.db.repository import collector_status_payload
        from pmanalysis.db.session import db_health

        while self._running:
            try:
                def _load():
                    health = db_health()
                    with get_session() as session:
                        payload = collector_status_payload(session)
                    return {**health, **payload}

                cached = await self._db_worker.run("stats-cache", _load)
                cached["feeds"] = self._feed_snapshot or self.snapshot_feeds()
                cached["last_tick_at"] = (
                    self.last_tick_at.isoformat() if self.last_tick_at else None
                )
                cached["last_discovery_at"] = (
                    self.last_discovery_at.isoformat() if self.last_discovery_at else None
                )
                cached["feed_record"] = FEED_RECORD_ENABLED
                cached["collector_running"] = True
                self.stats_cache = cached
            except Exception as exc:
                log.warning("stats cache refresh failed: %r", exc, exc_info=True)
            await asyncio.sleep(10)

    async def _run_finalize(self) -> list:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        # finalize_expired awaits Gamma HTTP — keep on main loop but yield often.
        with get_session() as session:
            finalized = await self.window_manager.finalize_expired(
                session, now_ts, WINDOW_ROLL_GRACE_SEC, self.chainlink
            )
            finalized_ids = [window.id for window in finalized]
        # Parquet export + feature extraction are blocking (SQLite read, file
        # write, pandas). Off the event loop, so the CLOB WebSocket reader keeps
        # up at the roll — when both intervals finalize and the CLOB bursts book
        # snapshots, a blocked loop gets the socket closed as a slow consumer.
        # One job per window lets the 1 Hz sampler interleave on the worker.
        for window_id in finalized_ids:
            await self._db_worker.run(
                "window-archive", lambda wid=window_id: self._archive_window_sync(wid)
            )
        return finalized

    def _archive_window_sync(self, window_id: int) -> None:
        from pmanalysis.db.models import Window

        with get_session() as session:
            window = session.get(Window, window_id)
            if window is None:
                return
            try:
                path = export_window_parquet(session, window)
                session.commit()  # the archive flag must not depend on features
                log.info("Archived %s -> %s", window.slug, path)
            except Exception as exc:
                session.rollback()
                log.warning("archive failed for %s: %r", window.slug, exc, exc_info=True)
            try:
                compute_window_features(session, window)
                session.commit()
            except Exception as exc:
                session.rollback()
                log.warning("features failed for %s: %r", window.slug, exc, exc_info=True)

    async def _resolution_recheck_loop(self) -> None:
        """Reconcile inferred outcomes with Gamma once it publishes a winner."""
        while self._running:
            await asyncio.sleep(RESOLUTION_RECHECK_INTERVAL_SEC)
            try:
                with get_session() as session:
                    changed = await self.window_manager.recheck_inferred(
                        session, int(time.time())
                    )
                    for window in changed:
                        if window.outcome_mismatch:
                            log.warning(
                                "Outcome mismatch %s: Gamma %s vs inferred %s",
                                window.slug,
                                window.outcome,
                                window.inferred_outcome,
                            )
            except Exception as exc:
                log.warning("resolution recheck failed: %r", exc, exc_info=True)

    async def _lead_lag_loop(self) -> None:
        while self._running:
            await asyncio.sleep(LEAD_LAG_REFRESH_SEC)
            try:
                payload = self.analytics.refresh_lead_lag(int(time.time()))
                await self.live_store.set_lead_lag(payload)
            except Exception as exc:
                log.warning("lead/lag refresh failed: %r", exc, exc_info=True)

    async def _oracle_broadcast_loop(self) -> None:
        """Backfill chart history from feed buffers (prices stream on each tick)."""
        while self._running:
            try:
                await self._enrich_oracle_histories()
                await self._enrich_binance_histories()
                await self._enrich_twap_histories()
            except Exception as exc:
                log.warning("oracle history enrich failed: %s", exc)
            await asyncio.sleep(2)

    async def _enrich_oracle_histories(self) -> None:
        cards = list((await self.live_store.get_snapshot_payload()).get("markets", []))
        for card in cards:
            asset = card.get("asset")
            interval = card.get("interval")
            start_ts = card.get("start_ts") or 0
            end_ts = card.get("end_ts") or 0
            if not asset or not start_ts or not end_ts:
                continue
            oracle_pts = self.chainlink.oracle_points_for_window(asset, start_ts, end_ts)
            await self.live_store.enrich_oracle_history(
                asset,
                interval,
                start_ts,
                end_ts,
                oracle_pts,
                price_at=lambda ts, a=asset: self.chainlink.get_price_at(a, ts),
            )

    async def _enrich_twap_histories(self) -> None:
        cards = list((await self.live_store.get_snapshot_payload()).get("markets", []))
        for card in cards:
            asset = card.get("asset")
            interval = card.get("interval")
            start_ts = card.get("start_ts") or 0
            end_ts = card.get("end_ts") or 0
            if not asset or not start_ts or not end_ts:
                continue
            twap_pts = self.chainlink.twap_points_for_window(
                asset, start_ts, end_ts, TWAP_WINDOW_SEC
            )
            if twap_pts:
                await self.live_store.enrich_twap_history(
                    asset, interval, start_ts, end_ts, twap_pts
                )

    async def _enrich_binance_histories(self) -> None:
        cards = list((await self.live_store.get_snapshot_payload()).get("markets", []))
        for card in cards:
            asset = card.get("asset")
            interval = card.get("interval")
            start_ts = card.get("start_ts") or 0
            end_ts = card.get("end_ts") or 0
            if not asset or not start_ts or not end_ts:
                continue
            binance_pts = self.binance.points_for_window(asset, start_ts, end_ts)
            await self.live_store.enrich_binance_history(
                asset, interval, start_ts, end_ts, binance_pts
            )

    async def _clob_loop(self) -> None:
        while self._running:
            try:
                await self.clob.run(self._get_token_ids, on_update=self._on_clob_update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("clob loop failed: %s", exc)
                await asyncio.sleep(5)

    async def _quote_push_loop(self) -> None:
        """Push YES/NO mids from the in-memory CLOB cache at high frequency."""
        while self._running:
            try:
                for window_id in list(self._active_window_ids):
                    await self._push_live_for_window_id(window_id)
            except Exception as exc:
                log.warning("quote push failed: %s", exc)
            await asyncio.sleep(QUOTE_PUSH_INTERVAL_SEC)

    async def _book_poll_loop(self) -> None:
        """REST fallback, but only for tokens whose WS quote has gone stale."""
        while self._running:
            try:
                if self._token_ids:
                    stale = self.clob.stale_tokens(self._token_ids, CLOB_STALE_SEC)
                    if stale:
                        changed = await self.clob.seed_tokens(stale)
                        await self._push_windows_for_tokens(changed)
            except Exception as exc:
                log.warning("book poll failed: %s", exc)
            await asyncio.sleep(BOOK_POLL_INTERVAL_SEC)

    def _get_token_ids(self) -> list[str]:
        return list(self._token_ids)

    def _enrich_clob_updates(self, updates: list[dict]) -> list[dict]:
        enriched: list[dict] = []
        for upd in updates:
            token_id = upd.get("token_id", "")
            if not token_id:
                continue
            row = dict(upd)
            window_id = self._token_to_window_id.get(token_id)
            row["window_id"] = window_id
            cached = self._window_cache.get(window_id) if window_id else None
            if cached:
                row["asset"] = cached.asset
                row["interval"] = cached.interval
                if token_id == cached.yes_token_id:
                    row["side"] = "YES"
                elif token_id == cached.no_token_id:
                    row["side"] = "NO"
            enriched.append(row)
        return enriched

    def _on_clob_update(self, updates: list[dict]) -> None:
        if not updates:
            return
        if FEED_RECORD_ENABLED:
            self._feed_recorder.record_clob(self._enrich_clob_updates(updates))
        token_ids = [u.get("token_id", "") for u in updates if u.get("token_id")]
        self._schedule_live_push(
            self._token_to_window_id[t] for t in token_ids if t in self._token_to_window_id
        )

    def _on_binance_tick(self, tick: dict) -> None:
        asset = tick.get("asset")
        price = tick.get("price")
        if not asset or price is None:
            return
        if FEED_RECORD_ENABLED:
            self._feed_recorder.record_binance(tick)
        self.analytics.on_binance(asset, tick.get("ts_ms") or int(time.time() * 1000), price)
        self._pending_binance_prices[asset] = price
        self._schedule_price_flush()

    def _on_oracle_tick(self, tick: dict) -> None:
        asset = tick.get("asset")
        price = tick.get("price")
        if not asset:
            return
        source = tick.get("source") or ""
        if FEED_RECORD_ENABLED and price is not None and not source.startswith("twap_"):
            self._feed_recorder.record_oracle(tick)
        if price is not None and source == "chainlink":
            self.analytics.on_chainlink(
                asset, tick.get("ts_ms") or int(time.time() * 1000), price
            )
            self._pending_oracle_prices[asset] = price
            self._schedule_price_flush()
        if price is not None and source.startswith("twap_"):
            self._twap_dirty = True
            self._schedule_price_flush()
        # Spot/TWAP UI uses oracle WS; skip tick coalesce to keep latency minimal.
        if source == "chainlink":
            self._opening_worker.schedule(asset)

    def _schedule_price_flush(self) -> None:
        """Coalesce bursty Binance/Chainlink ticks into one push instead of
        spawning a task per tick (Binance alone can be tens of msgs/sec across
        3 symbols; a create_task-per-tick storm is what starves the event loop
        and lets the CLOB WebSocket reader fall behind -> slow-consumer drops)."""
        if self._price_flush_task is None or self._price_flush_task.done():
            self._price_flush_task = asyncio.create_task(
                self._flush_prices(), name="price-flush"
            )

    async def _flush_prices(self) -> None:
        try:
            await asyncio.sleep(0.05)
            oracle = dict(self._pending_oracle_prices)
            self._pending_oracle_prices.clear()
            binance = dict(self._pending_binance_prices)
            self._pending_binance_prices.clear()
            twap_dirty = self._twap_dirty
            self._twap_dirty = False
            if oracle:
                await self.live_store.set_oracle_prices(oracle)
            if binance:
                await self.live_store.set_binance_prices(binance)
            if twap_dirty:
                await self.live_store.set_twap_by_window(self.chainlink.twap_by_window())
        except asyncio.CancelledError:
            raise

    async def _capture_openings_for_asset(self, asset: str) -> None:
        changed = False
        with get_session() as session:
            changed = await self.window_manager.capture_openings_for_asset(
                session, asset, self.chainlink
            )
            if changed:
                self._refresh_window_cache(session)
        if changed:
            for window_id in self._window_ids_by_asset.get(asset, []):
                cached = self._window_cache.get(window_id)
                if cached:
                    self._last_live_push_ms.pop(f"{cached.asset}:{cached.interval}", None)
            self._schedule_live_push(self._window_ids_by_asset.get(asset, []))

    def _schedule_live_push(self, window_ids) -> None:
        for window_id in window_ids:
            if window_id:
                self._pending_push_ids.add(window_id)
        if not self._pending_push_ids:
            return
        if self._push_coalesce_task is None or self._push_coalesce_task.done():
            self._push_coalesce_task = asyncio.create_task(
                self._flush_live_pushes(), name="live-push-flush"
            )

    async def _flush_live_pushes(self) -> None:
        try:
            delay = LIVE_PUSH_MIN_INTERVAL_MS / 1000.0
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Yield once so concurrent feed callbacks can coalesce into this flush.
                await asyncio.sleep(0)
            window_ids = list(self._pending_push_ids)
            self._pending_push_ids.clear()
            for window_id in window_ids:
                try:
                    await self._push_live_for_window_id(window_id)
                except Exception as exc:
                    log.warning("live push failed for window %s: %s", window_id, exc)
        except asyncio.CancelledError:
            raise

    async def _push_windows_for_tokens(self, token_ids: list[str]) -> None:
        touched: set[int] = set()
        for token_id in token_ids:
            window_id = self._token_to_window_id.get(token_id)
            if window_id:
                touched.add(window_id)
        self._schedule_live_push(touched)

    async def _push_live_for_window_id(self, window_id: int) -> None:
        window = self._window_cache.get(window_id)
        if not window:
            return

        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._note_candle(window)
        key = f"{window.asset}:{window.interval}"
        last = self._last_live_push_ms.get(key, 0)
        if LIVE_PUSH_MIN_INTERVAL_MS > 0 and ts_ms - last < LIVE_PUSH_MIN_INTERVAL_MS:
            return
        self._last_live_push_ms[key] = ts_ms

        state = self.builder.build(window, ts_ms)
        card, point = state.card(), state.point()
        self._note_candle(window, card)
        await self.live_store.push_tick(card.asset, card.interval, card, point)

    def _note_candle(self, window: CachedWindow, card=None) -> None:
        price = None
        open_px = window.opening_oracle_price or window.strike_price
        if card is not None:
            price = card.oracle_price if card.oracle_price is not None else card.twap_oracle
            open_px = card.twap_initial or card.opening_oracle_price or card.strike_price or open_px
        elif self.chainlink:
            price = self.chainlink.get_price(window.asset)
        if price is None or not window.start_ts:
            return
        self.candles.update(window.asset, window.interval, window.start_ts, price, open_px)
