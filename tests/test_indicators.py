from pmanalysis.feeds.gamma import strike_to_float
from pmanalysis.feeds.slugs import boundary_ts
from pmanalysis.analytics.live import LiveAnalytics
from pmanalysis.indicators.compute import compute_tick_fields, win_rate


def test_boundary_ts_5m():
    assert boundary_ts(1780338799, "5m") == 1780338600


def test_strike_to_float():
    assert strike_to_float("$97,432.50") == 97432.5


def test_compute_tick_fields():
    fields = compute_tick_fields(
        yes_ask=52.0,
        yes_bid=50.0,
        no_ask=49.0,
        no_bid=47.0,
        oracle_price=97450.0,
        strike_price=97432.0,
        secs_remaining=120,
    )
    assert fields["combined_ask"] == 101.0
    assert fields["dist_from_strike"] == 18.0


def test_sigma_ignores_push_rate():
    """Repeated same-second updates must not distort the per-second volatility."""
    once, burst = LiveAnalytics(("BTC",)), LiveAnalytics(("BTC",))
    for sec in range(0, 300):
        price = 100.0 + (sec % 2) * 0.1
        once.on_chainlink("BTC", sec * 1000, price)
        for _ in range(5):  # five live pushes per second
            burst.on_chainlink("BTC", sec * 1000, price)
    assert once.sigma("BTC", 299) is not None
    assert burst.sigma("BTC", 299) == once.sigma("BTC", 299)


def test_win_rate():
    assert win_rate(["UP", "UP", "DOWN"]) == 66.7
