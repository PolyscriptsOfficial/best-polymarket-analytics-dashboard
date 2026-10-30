import asyncio

from pmanalysis import config
from pmanalysis.analytics.live import LiveAnalytics
from pmanalysis.collector.alerts import AlertEngine, evaluate_market
from pmanalysis.collector.snapshot import SnapshotBuilder
from pmanalysis.collector.window_cache import CachedWindow


class FakeChainlink:
    def get_price(self, asset):
        return 100_050.0  # $50 above the opening TWAP

    def get_twap(self, asset, window_s=None):
        return 100_010.0

    def get_twap_opening(self, asset, start_ts, window_s=None):
        return 100_000.0


class FakeClob:
    def __init__(self, quotes, ages):
        self.quotes, self.ages = quotes, ages

    def get_quote(self, token_id):
        return self.quotes.get(token_id, (0.0, 0.0))

    def quote_age_sec(self, token_id):
        return self.ages.get(token_id)

    def book_stats(self, token_id):
        return {"ask_size": 50.0, "bid_size": 40.0, "ask_depth": 120.0, "bid_depth": 90.0}

    def last_trade(self, token_id):
        return None

    def traded_notional(self, token_id):
        return 0.0


NOW = 1_800_000_000
WINDOW = CachedWindow(
    id=1, asset="BTC", interval="5m", slug="btc-updown-5m-x", question="",
    yes_token_id="y", no_token_id="n", start_ts=NOW - 150, end_ts=NOW + 150,
    strike_price=None, opening_oracle_price=None, twap_initial=100_000.0, outcome=None,
)


def _builder(quotes, ages, spot_last_sec=NOW):
    analytics = LiveAnalytics(("BTC",))
    # Flat-ish tape so sigma exists; last sample at spot_last_sec.
    for i in range(120, -1, -1):
        sec = spot_last_sec - i
        analytics.on_chainlink("BTC", sec * 1000, 100_000.0 + (i % 2))
    return SnapshotBuilder(FakeChainlink(), FakeClob(quotes, ages), analytics)


def test_edge_and_arb_alerts_on_fresh_inputs():
    # Spot +$50 over target with 150s left on ~$1/sqrt(s) vol: fair YES ~100, YES ask 60.
    builder = _builder({"y": (60.0, 58.0), "n": (38.0, 36.0)}, {"y": 1.0, "n": 1.0})
    state = builder.build(WINDOW, NOW * 1000)
    assert state.fresh
    kinds = {(a.kind, a.key) for a in evaluate_market(state)}
    assert ("edge", "edge:btc-updown-5m-x:YES") in kinds
    assert ("arb", "arb:btc-updown-5m-x") in kinds  # 60 + 38 = 98


def test_stale_quote_or_spot_suppresses_market_alerts():
    quotes = {"y": (60.0, 58.0), "n": (38.0, 36.0)}
    stale_book = _builder(quotes, {"y": 1.0, "n": config.ALERT_MAX_QUOTE_AGE_SEC + 1})
    state = stale_book.build(WINDOW, NOW * 1000)
    assert not state.fresh and evaluate_market(state) == []

    stale_spot = _builder(quotes, {"y": 1.0, "n": 1.0}, spot_last_sec=NOW - 30)
    state = stale_spot.build(WINDOW, NOW * 1000)
    assert not state.fresh and evaluate_market(state) == []
    assert state.fair_yes is not None  # the HUD still shows the model


def test_no_alerts_near_the_bell():
    builder = _builder({"y": (60.0, 58.0), "n": (38.0, 36.0)}, {"y": 1.0, "n": 1.0})
    late = (WINDOW.end_ts - config.ALERT_MIN_SECS_LEFT + 1) * 1000
    assert evaluate_market(builder.build(WINDOW, late)) == []


def test_alert_engine_cooldown(monkeypatch):
    monkeypatch.setattr(config, "ALERT_DISCORD_WEBHOOK", "")
    monkeypatch.setattr(config, "ALERT_TELEGRAM_TOKEN", "")
    builder = _builder({"y": (60.0, 58.0), "n": (38.0, 36.0)}, {"y": 1.0, "n": 1.0})
    alerts = evaluate_market(builder.build(WINDOW, NOW * 1000))
    published = []

    async def publish(payload):
        published.append(payload)

    async def run():
        engine = AlertEngine(publish)
        await engine.emit(alerts)
        await engine.emit(alerts)  # same keys inside the cooldown: dropped
        await engine.close()

    asyncio.run(run())
    assert len(published) == len(alerts)
    assert all(p["type"] == "alert" for p in published)
