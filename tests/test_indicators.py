from pmanalysis.feeds.gamma import boundary_ts, strike_to_float
from pmanalysis.indicators.compute import IndicatorState, compute_tick_fields, win_rate


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


def test_indicator_state_momentum():
    state = IndicatorState()
    state.add_oracle(1_000, 100.0)
    state.add_oracle(61_000, 101.5)
    out = state.compute(
        ts_ms=61_000,
        yes_mid=0.52,
        no_mid=0.48,
        combined_ask=100.0,
        window_duration=300,
        secs_remaining=100,
    )
    assert out["momentum_1m"] == 1.5


def test_win_rate():
    assert win_rate(["UP", "UP", "DOWN"]) == 66.7
