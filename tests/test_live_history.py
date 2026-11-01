"""Live chart history: one merged point per second, sized for a full 15m window."""

import asyncio

from pmanalysis.core.live_store import HISTORY_MAX_SEC, LiveStore, TickPoint
from pmanalysis.schemas import MarketCard

START = 1_800_000_000


def _card(start=START, interval="15m"):
    return MarketCard(
        asset="BTC", interval=interval, slug=f"btc-updown-{interval}-{start}", question="",
        yes_mid=0.5, no_mid=0.5, yes_ask=51, no_ask=50, combined_ask=101, oracle_price=100.0,
        strike_price=None, opening_oracle_price=None, dist_from_strike=None, dist_from_opening=None,
        start_ts=start, end_ts=start + 900, secs_remaining=0, outcome=None, arb_edge=None, lean=None,
    )


def _point(ts_ms, yes=51.0, oracle=100.0):
    return TickPoint(
        ts_ms=ts_ms, oracle_price=oracle, yes_mid=yes / 100, no_mid=0.5, combined_ask=101.0,
        dist_from_strike=None, arb_edge=None, lean=None, yes_ask=yes, no_ask=50.0,
    )


def test_full_15m_window_survives_bursty_pushes():
    store, card = LiveStore(), _card()

    async def run():
        for sec in range(900):
            for k in range(6):  # several live pushes per second
                await store.push_tick("BTC", "15m", card, _point((START + sec) * 1000 + k * 150, yes=50 + k))
        # Feed backfill with many Binance trades per second must not evict quotes.
        await store.enrich_binance_history(
            "BTC", "15m", START, START + 900,
            [{"ts_ms": (START + i // 20) * 1000 + i % 20, "binance_price": 101.0} for i in range(900 * 20)],
        )
        return await store.get_history_payload("BTC", "15m")

    payload = asyncio.run(run())
    points = payload["points"]
    assert len(points) == 900
    assert points[0]["ts_ms"] // 1000 == START and points[-1]["ts_ms"] // 1000 == START + 899
    assert all(p["yes_ask"] == 55 for p in points)  # latest push in each second wins
    assert all(p["binance_price"] == 101.0 for p in points)  # filled in place
    assert HISTORY_MAX_SEC >= 900


def test_load_history_merges_with_live_points():
    store, card = LiveStore(), _card()

    async def run():
        await store.push_tick("BTC", "15m", card, _point((START + 500) * 1000, yes=70))
        stored = [_point((START + s) * 1000, yes=60) for s in range(0, 501)]
        await store.load_history("BTC", "15m", stored)
        await store.enrich_twap_history(
            "BTC", "15m", START, START + 900, [{"ts_ms": START * 1000, "twap_oracle": 99.5}]
        )
        return await store.get_history_payload("BTC", "15m")

    points = asyncio.run(run())["points"]
    assert len(points) == 501
    assert points[0]["yes_ask"] == 60 and points[-1]["yes_ask"] == 70  # live push kept
    assert all(p["twap_oracle"] == 99.5 for p in points)  # nearest prior TWAP carried
