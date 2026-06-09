"""Always-on collector orchestrator."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pmanalysis.collector.sampler import TickSampler
from pmanalysis.collector.window_manager import WindowManager
from pmanalysis.config import (
    COLLECTOR_TICK_INTERVAL_SEC,
    DISCOVERY_INTERVAL_SEC,
    STRIKE_POLL_INTERVAL_SEC,
    WINDOW_ROLL_GRACE_SEC,
)
from pmanalysis.core.live_store import LiveStore, get_live_store
from pmanalysis.db.session import get_session, init_db
from pmanalysis.export.parquet import export_window_parquet
from pmanalysis.feeds.chainlink import ChainlinkFeed
from pmanalysis.feeds.clob import ClobFeed
from pmanalysis.feeds.gamma import GammaClient

log = logging.getLogger(__name__)


class CollectorService:
    def __init__(self, live_store: LiveStore | None = None) -> None:
        self.live_store = live_store or get_live_store()
        self.gamma = GammaClient()
        self.chainlink = ChainlinkFeed()
        self.clob = ClobFeed()
        self.window_manager = WindowManager(self.gamma)
        self.sampler = TickSampler(
            self.window_manager, self.chainlink, self.clob, self.live_store
        )
        self._tasks: list[asyncio.Task] = []
        self._token_ids: list[str] = []
        self._running = False
        self.last_tick_at: datetime | None = None
        self.last_discovery_at: datetime | None = None

    async def start(self) -> None:
        init_db()
        with get_session() as session:
            self.window_manager.load_active_from_db(session)

        self._running = True
        await self._discover_once()
        self._tasks = [
            asyncio.create_task(self.chainlink.run(), name="chainlink"),
            asyncio.create_task(self._oracle_broadcast_loop(), name="oracle-broadcast"),
            asyncio.create_task(self._clob_loop(), name="clob"),
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._strike_loop(), name="strike"),
            asyncio.create_task(self._sampler_loop(), name="sampler"),
            asyncio.create_task(self._finalize_loop(), name="finalize"),
        ]
        log.info("Collector started with %d token subscriptions", len(self._token_ids))
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.gamma.close()
        await self.clob.close()
        await self.chainlink.close()

    async def _discover_once(self) -> None:
        discovered = await self.gamma.discover_active_windows()
        with get_session() as session:
            self._token_ids = await self.window_manager.sync_discovered(session, discovered)
        self.last_discovery_at = datetime.now(timezone.utc)
        if self._token_ids:
            await self.clob.seed_tokens(self._token_ids)

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                await self._discover_once()
            except Exception as exc:
                log.warning("discovery failed: %s", exc)
            await asyncio.sleep(DISCOVERY_INTERVAL_SEC)

    async def _strike_loop(self) -> None:
        while self._running:
            try:
                with get_session() as session:
                    await self.window_manager.refresh_strikes(session)
            except Exception as exc:
                log.warning("strike poll failed: %s", exc)
            await asyncio.sleep(STRIKE_POLL_INTERVAL_SEC)

    async def _sampler_loop(self) -> None:
        while self._running:
            try:
                with get_session() as session:
                    samples = self.sampler.sample_all(session)
                    for sample in samples:
                        await self.live_store.push_tick(
                            sample.asset, sample.interval, sample.card, sample.point
                        )
                    if samples:
                        self.last_tick_at = datetime.now(timezone.utc)
            except Exception as exc:
                log.warning("sampler failed: %s", exc)
            await asyncio.sleep(COLLECTOR_TICK_INTERVAL_SEC)

    async def _finalize_loop(self) -> None:
        while self._running:
            try:
                now_ts = int(datetime.now(timezone.utc).timestamp())
                with get_session() as session:
                    finalized = await self.window_manager.finalize_expired(
                        session, now_ts, WINDOW_ROLL_GRACE_SEC
                    )
                    for window in finalized:
                        try:
                            path = export_window_parquet(session, window)
                            log.info("Archived %s -> %s", window.slug, path)
                        except Exception as exc:
                            log.warning("archive failed for %s: %s", window.slug, exc)
                if finalized:
                    await self._discover_once()
            except Exception as exc:
                log.warning("finalize failed: %s", exc)
            await asyncio.sleep(5)

    async def _oracle_broadcast_loop(self) -> None:
        while self._running:
            try:
                await self.live_store.set_oracle_prices(dict(self.chainlink.prices))
            except Exception as exc:
                log.warning("oracle broadcast failed: %s", exc)
            await asyncio.sleep(1)

    async def _clob_loop(self) -> None:
        while self._running:
            if not self._token_ids:
                await asyncio.sleep(2)
                continue
            try:
                await self.clob.run(self._token_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("clob loop failed: %s", exc)
                await asyncio.sleep(5)
