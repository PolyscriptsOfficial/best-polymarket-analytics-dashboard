"""One market's merged state at one instant.

Shared by the 1 Hz sampler (persists one Tick row) and the sub-second live
push (MarketCard/TickPoint over WebSocket), so both paths compute every field
the same way. Only ``tick_columns()`` is stored; see db/tick_fields.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pmanalysis.analytics.fair_value import edges, fair_yes_cents
from pmanalysis.analytics.live import LiveAnalytics
from pmanalysis.config import (
    ALERT_MAX_QUOTE_AGE_SEC,
    ALERT_MAX_SPOT_AGE_SEC,
    TWAP_WINDOW_SEC,
)
from pmanalysis.core.live_store import TickPoint
from pmanalysis.feeds.binance import BinanceFeed
from pmanalysis.feeds.chainlink import ChainlinkFeed
from pmanalysis.feeds.clob import ClobFeed
from pmanalysis.indicators.compute import compute_tick_fields
from pmanalysis.schemas import MarketCard


class WindowLike(Protocol):
    """Window ORM row or CachedWindow."""

    id: int
    asset: str
    interval: str
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    start_ts: int
    end_ts: int
    strike_price: float | None
    opening_oracle_price: float | None
    twap_initial: float | None
    outcome: str | None


def _diff(current: float | None, base: float | None) -> float | None:
    if current is None or base is None:
        return None
    return round(current - base, 2)


_EMPTY_BOOK = {"ask_size": None, "bid_size": None, "ask_depth": None, "bid_depth": None}


@dataclass(slots=True)
class MarketState:
    window: WindowLike
    ts_ms: int
    secs_remaining: int
    yes_ask: float
    yes_bid: float
    no_ask: float
    no_bid: float
    oracle_price: float | None
    binance_price: float | None
    twap_oracle: float | None
    twap_initial: float | None
    twap_diff: float | None
    fields: dict  # compute_tick_fields(): mids, combined ask, spread, dist_from_strike
    sigma: float | None
    fair_yes: float | None
    edge_yes: float | None
    edge_no: float | None
    yes_book: dict
    no_book: dict
    yes_last_trade: float | None
    no_last_trade: float | None
    volume_usd: float  # cumulative YES+NO notional since subscription
    fresh: bool = True  # both quotes and Chainlink spot are recent (alerts only)

    @property
    def has_clob(self) -> bool:
        return self.yes_ask > 0 or self.yes_bid > 0 or self.no_ask > 0 or self.no_bid > 0

    @property
    def has_data(self) -> bool:
        return self.has_clob or self.oracle_price is not None or self.binance_price is not None

    @property
    def arb_edge(self) -> float:
        return round(100 - self.fields["combined_ask"], 2)

    @property
    def lean(self) -> float:
        return round(self.fields["yes_mid"] - self.fields["no_mid"], 4)

    def card(self) -> MarketCard:
        w = self.window
        f = self.fields
        return MarketCard(
            asset=w.asset,
            interval=w.interval,
            slug=w.slug,
            question=w.question or "",
            yes_mid=f["yes_mid"],
            no_mid=f["no_mid"],
            yes_ask=self.yes_ask,
            no_ask=self.no_ask,
            combined_ask=f["combined_ask"],
            oracle_price=self.oracle_price,
            strike_price=w.strike_price,
            opening_oracle_price=w.opening_oracle_price or w.strike_price,
            dist_from_strike=f["dist_from_strike"],
            dist_from_opening=self.twap_diff,
            start_ts=w.start_ts,
            end_ts=w.end_ts,
            secs_remaining=self.secs_remaining,
            outcome=w.outcome,
            arb_edge=self.arb_edge,
            lean=self.lean,
            twap_oracle=self.twap_oracle,
            twap_initial=self.twap_initial,
            fair_yes=self.fair_yes,
            edge_yes=self.edge_yes,
            edge_no=self.edge_no,
            sigma=None if self.sigma is None else round(self.sigma, 4),
            yes_ask_size=self.yes_book["ask_size"],
            no_ask_size=self.no_book["ask_size"],
            yes_ask_depth=self.yes_book["ask_depth"],
            no_ask_depth=self.no_book["ask_depth"],
            volume_usd=round(self.volume_usd, 2),
        )

    def point(self) -> TickPoint:
        f = self.fields
        return TickPoint(
            ts_ms=self.ts_ms,
            oracle_price=self.oracle_price,
            binance_price=self.binance_price,
            yes_mid=f["yes_mid"],
            no_mid=f["no_mid"],
            yes_ask=self.yes_ask,
            yes_bid=self.yes_bid,
            no_ask=self.no_ask,
            no_bid=self.no_bid,
            combined_ask=f["combined_ask"],
            dist_from_strike=f["dist_from_strike"],
            arb_edge=self.arb_edge,
            lean=self.lean,
            twap_oracle=self.twap_oracle,
            twap_diff=self.twap_diff,
            fair_yes=self.fair_yes,
        )

    def tick_columns(self) -> dict:
        """The stored Tick row (traded_usd is set by the sampler as a per-second delta)."""
        return {
            "window_id": self.window.id,
            "ts_ms": self.ts_ms,
            "oracle_price": self.oracle_price,
            "binance_price": self.binance_price,
            "twap_oracle": self.twap_oracle,
            "yes_ask": self.yes_ask,
            "yes_bid": self.yes_bid,
            "no_ask": self.no_ask,
            "no_bid": self.no_bid,
            "yes_ask_size": self.yes_book["ask_size"],
            "yes_bid_size": self.yes_book["bid_size"],
            "no_ask_size": self.no_book["ask_size"],
            "no_bid_size": self.no_book["bid_size"],
            "yes_ask_depth": self.yes_book["ask_depth"],
            "yes_bid_depth": self.yes_book["bid_depth"],
            "no_ask_depth": self.no_book["ask_depth"],
            "no_bid_depth": self.no_book["bid_depth"],
            "yes_last_trade": self.yes_last_trade,
            "no_last_trade": self.no_last_trade,
            "fair_yes": self.fair_yes,
        }


class SnapshotBuilder:
    def __init__(
        self,
        chainlink: ChainlinkFeed,
        clob: ClobFeed,
        analytics: LiveAnalytics,
        binance: BinanceFeed | None = None,
    ) -> None:
        self.chainlink = chainlink
        self.clob = clob
        self.analytics = analytics
        self.binance = binance

    def build(self, window: WindowLike, ts_ms: int) -> MarketState:
        now_sec = ts_ms // 1000
        asset = window.asset
        yes_ask, yes_bid = self.clob.get_quote(window.yes_token_id) if window.yes_token_id else (0.0, 0.0)
        no_ask, no_bid = self.clob.get_quote(window.no_token_id) if window.no_token_id else (0.0, 0.0)
        oracle_price = self.chainlink.get_price(asset)
        binance_price = self.binance.get_price(asset) if self.binance else None
        secs_remaining = max(0, window.end_ts - now_sec)
        fields = compute_tick_fields(
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            oracle_price=oracle_price,
            strike_price=window.strike_price,
            secs_remaining=secs_remaining,
        )

        twap_oracle = self.chainlink.get_twap(asset, TWAP_WINDOW_SEC)
        # Target = TWAP at window open only (never live TWAP / spot). The DB copy
        # covers restarts after the RTDS boundary sample has rolled off.
        twap_initial = (
            self.chainlink.get_twap_opening(asset, window.start_ts, TWAP_WINDOW_SEC)
            or window.twap_initial
        )

        sigma = self.analytics.sigma(asset, now_sec)
        known_avg = None
        if secs_remaining < TWAP_WINDOW_SEC:
            known_avg = self.analytics.mean_price(
                asset, window.end_ts - TWAP_WINDOW_SEC, now_sec
            )
        fair_yes = fair_yes_cents(
            spot=oracle_price,
            target=twap_initial,
            secs_remaining=secs_remaining,
            sigma=sigma,
            known_avg=known_avg,
            twap_window=TWAP_WINDOW_SEC,
        )
        edge_yes, edge_no = edges(fair_yes, yes_ask, no_ask)

        yes_token, no_token = window.yes_token_id, window.no_token_id
        return MarketState(
            window=window,
            ts_ms=ts_ms,
            secs_remaining=secs_remaining,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            oracle_price=oracle_price,
            binance_price=binance_price,
            twap_oracle=twap_oracle,
            twap_initial=twap_initial,
            twap_diff=_diff(twap_oracle, twap_initial),
            fields=fields,
            sigma=sigma,
            fair_yes=fair_yes,
            edge_yes=edge_yes,
            edge_no=edge_no,
            yes_book=self.clob.book_stats(yes_token) if yes_token else _EMPTY_BOOK,
            no_book=self.clob.book_stats(no_token) if no_token else _EMPTY_BOOK,
            yes_last_trade=self.clob.last_trade(yes_token) if yes_token else None,
            no_last_trade=self.clob.last_trade(no_token) if no_token else None,
            volume_usd=(
                (self.clob.traded_notional(yes_token) if yes_token else 0.0)
                + (self.clob.traded_notional(no_token) if no_token else 0.0)
            ),
            fresh=self._inputs_fresh(asset, now_sec, yes_token, no_token),
        )

    def _inputs_fresh(self, asset: str, now_sec: int, *token_ids: str) -> bool:
        spot_age = self.analytics.chainlink_age(asset, now_sec)
        if spot_age is None or spot_age > ALERT_MAX_SPOT_AGE_SEC:
            return False
        for token_id in token_ids:
            age = self.clob.quote_age_sec(token_id) if token_id else None
            if age is None or age > ALERT_MAX_QUOTE_AGE_SEC:
                return False
        return True
