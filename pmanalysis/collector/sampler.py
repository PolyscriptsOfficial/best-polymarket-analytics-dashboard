"""Sample merged feed state into tick rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from pmanalysis.collector.window_manager import WindowManager
from pmanalysis.core.live_store import LiveStore, TickPoint, get_live_store
from pmanalysis.db.models import Indicator, Tick
from pmanalysis.feeds.chainlink import ChainlinkFeed
from pmanalysis.feeds.clob import ClobFeed
from pmanalysis.indicators.compute import IndicatorState, compute_indicators, compute_tick_fields
from pmanalysis.schemas import MarketCard


@dataclass
class SampleResult:
    asset: str
    interval: str
    card: MarketCard
    point: TickPoint


class TickSampler:
    def __init__(
        self,
        window_manager: WindowManager,
        chainlink: ChainlinkFeed,
        clob: ClobFeed,
        live_store: LiveStore | None = None,
    ) -> None:
        self.window_manager = window_manager
        self.chainlink = chainlink
        self.clob = clob
        self.live_store = live_store or get_live_store()
        self._indicator_states: dict[int, IndicatorState] = {}

    def _state_for(self, window_id: int) -> IndicatorState:
        if window_id not in self._indicator_states:
            self._indicator_states[window_id] = IndicatorState()
        return self._indicator_states[window_id]

    def sample_all(self, session: Session) -> list[SampleResult]:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        ts_ms = now_ts * 1000
        results: list[SampleResult] = []
        for window in self.window_manager.get_active_windows(session):
            yes_ask, yes_bid = self.clob.get_quote(window.yes_token_id)
            no_ask, no_bid = self.clob.get_quote(window.no_token_id)
            oracle_price = self.chainlink.get_price(window.asset)
            secs_remaining = max(0, window.end_ts - now_ts)
            tick_fields = compute_tick_fields(
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                no_ask=no_ask,
                no_bid=no_bid,
                oracle_price=oracle_price,
                strike_price=window.strike_price,
                secs_remaining=secs_remaining,
            )
            tick = Tick(
                window_id=window.id,
                ts_ms=ts_ms,
                oracle_price=oracle_price,
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                no_ask=no_ask,
                no_bid=no_bid,
                **tick_fields,
            )
            session.add(tick)

            ind_fields = compute_indicators(
                self._state_for(window.id),
                ts_ms=ts_ms,
                oracle_price=oracle_price,
                yes_mid=tick_fields["yes_mid"],
                no_mid=tick_fields["no_mid"],
                combined_ask=tick_fields["combined_ask"],
                window_duration=self.window_manager.window_duration(window),
                secs_remaining=secs_remaining,
            )
            session.add(Indicator(window_id=window.id, ts_ms=ts_ms, **ind_fields))

            card = MarketCard(
                asset=window.asset,
                interval=window.interval,
                slug=window.slug,
                question=window.question,
                yes_mid=tick_fields["yes_mid"],
                no_mid=tick_fields["no_mid"],
                yes_ask=yes_ask,
                no_ask=no_ask,
                combined_ask=tick_fields["combined_ask"],
                oracle_price=oracle_price,
                strike_price=window.strike_price,
                dist_from_strike=tick_fields["dist_from_strike"],
                secs_remaining=secs_remaining,
                outcome=window.outcome,
                arb_edge=ind_fields["arb_edge"],
                lean=ind_fields["lean"],
            )
            point = TickPoint(
                ts_ms=ts_ms,
                oracle_price=oracle_price,
                yes_mid=tick_fields["yes_mid"],
                no_mid=tick_fields["no_mid"],
                combined_ask=tick_fields["combined_ask"],
                dist_from_strike=tick_fields["dist_from_strike"],
                arb_edge=ind_fields["arb_edge"],
                lean=ind_fields["lean"],
            )
            results.append(SampleResult(window.asset, window.interval, card, point))
        if results:
            session.commit()
        return results
