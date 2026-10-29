"""Sample merged feed state into 1-second tick rows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from pmanalysis.collector.snapshot import MarketState, SnapshotBuilder
from pmanalysis.collector.window_cache import CachedWindow
from pmanalysis.collector.window_manager import WindowManager
from pmanalysis.core.live_store import TickPoint
from pmanalysis.db.models import Tick
from pmanalysis.schemas import MarketCard


@dataclass
class SampleResult:
    asset: str
    interval: str
    card: MarketCard
    point: TickPoint
    state: MarketState


class TickSampler:
    def __init__(self, window_manager: WindowManager, builder: SnapshotBuilder) -> None:
        self.window_manager = window_manager
        self.builder = builder
        # Cumulative traded notional per window at the previous sample, so each
        # tick stores only that second's volume.
        self._volume_seen: dict[int, float] = {}

    def drop_state(self, window_id: int | None) -> None:
        """Forget per-window state once a window rolls off."""
        if window_id is not None:
            self._volume_seen.pop(window_id, None)

    def sample_all(self, session: Session, now_sec: int) -> list[SampleResult]:
        """Write one Tick per active window, stamped on ``now_sec``.

        The caller schedules this on whole-second boundaries; using that second
        for both ``ts_ms`` and ``secs_remaining`` keeps the 1s grain exact.
        """
        ts_ms = now_sec * 1000
        results: list[SampleResult] = []
        for window in self.window_manager.get_active_windows(session):
            # Frozen snapshot: results outlive this session (alerts read them later).
            state = self.builder.build(CachedWindow.from_row(window), ts_ms)
            if not state.has_data:
                continue

            prev_volume = self._volume_seen.get(window.id)
            self._volume_seen[window.id] = state.volume_usd
            traded = None if prev_volume is None else max(0.0, state.volume_usd - prev_volume)

            session.add(Tick(**state.tick_columns(), traded_usd=None if traded is None else round(traded, 2)))
            if state.twap_initial is not None and state.twap_initial != window.twap_initial:
                # Persist the settlement target so History, backtests and outcome
                # inference survive restarts and RTDS buffer roll-off. Kept in
                # step with the live value: the exact boundary sample can land a
                # second or two after the open and replace a provisional one.
                window.twap_initial = state.twap_initial
            results.append(
                SampleResult(window.asset, window.interval, state.card(), state.point(), state)
            )
        if results:
            session.commit()
        return results
