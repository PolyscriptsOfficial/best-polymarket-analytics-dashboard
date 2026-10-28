import math

from pmanalysis.analytics.fair_value import edges, prob_up, sigma_per_sec
from pmanalysis.analytics.features import checkpoints_for, extract_features
from pmanalysis.analytics.lead_lag import estimate_lead_lag
from pmanalysis.analytics.tape import SecondTape


def test_tape_buckets_by_second_and_forward_fills():
    tape = SecondTape(maxlen=10)
    tape.add(1_000, 100.0)
    tape.add(1_900, 101.0)  # same second: last price wins
    tape.add(3_100, 103.0)
    tape.add(2_000, 99.0)  # out of order: ignored
    assert len(tape) == 2
    assert tape.dense(0, 4) == [None, 101.0, 101.0, 103.0, 103.0]


def test_prob_up_symmetry_and_monotonicity():
    base = dict(target=100.0, secs_remaining=240, sigma=0.05)
    assert prob_up(spot=100.0, **base) == 0.5
    assert prob_up(spot=100.5, **base) > 0.5 > prob_up(spot=99.5, **base)
    # Less time left -> more certain for the same lead.
    assert prob_up(spot=100.5, target=100.0, secs_remaining=90, sigma=0.05) > prob_up(spot=100.5, **base)


def test_prob_up_inside_twap_window_uses_known_average():
    # 10s left: 50 of 60 TWAP seconds are known and well above target.
    p = prob_up(spot=99.0, target=100.0, secs_remaining=10, sigma=0.05, known_avg=101.0)
    assert p > 0.99
    # At the bell the result is decided by the known average alone.
    assert prob_up(spot=50.0, target=100.0, secs_remaining=0, sigma=1.0, known_avg=100.0) == 0.999


def test_prob_up_missing_inputs():
    assert prob_up(spot=None, target=100.0, secs_remaining=60, sigma=0.1) is None
    assert prob_up(spot=100.0, target=100.0, secs_remaining=60, sigma=None) is None


def test_sigma_per_sec_scales_by_price():
    prices = [100.0 * math.exp(0.001 * (1 if i % 2 else -1)) for i in range(61)]
    sigma = sigma_per_sec(prices, min_samples=30)
    assert sigma is not None and abs(sigma - 0.2) < 0.01  # ~2*0.001*100
    assert sigma_per_sec(prices[:10], min_samples=30) is None


def test_edges():
    assert edges(60.0, yes_ask=55.0, no_ask=46.0) == (5.0, -6.0)
    assert edges(None, 55.0, 46.0) == (None, None)
    assert edges(60.0, 0.0, 46.0) == (None, -6.0)


def test_lead_lag_detects_known_delay():
    import random

    rng = random.Random(7)
    binance = [100.0]
    for _ in range(400):
        binance.append(binance[-1] * math.exp(rng.gauss(0, 0.001)))
    lag = 3
    chainlink = [binance[0]] * lag + binance[:-lag]
    result = estimate_lead_lag(binance, chainlink, max_lag=8)
    assert result is not None
    assert result.lag_sec == lag
    assert result.corr > 0.95


def _window_rows(start_ts: int, duration: int, *, base: float, drift: float) -> list[dict]:
    rows = []
    for i in range(duration + 1):
        price = base + drift * i
        rows.append(
            {
                "ts_ms": (start_ts + i) * 1000,
                "oracle_price": price + (0.3 if i % 2 else -0.3),
                "twap_oracle": price,
                "yes_ask": 60.0,
                "yes_bid": 58.0,
                "no_ask": 42.0,
                "no_bid": 40.0,
            }
        )
    return rows


def test_extract_features_checkpoints_and_backfilled_fair():
    start = 1_800_000_000
    rows = _window_rows(start, 300, base=100.0, drift=0.01)
    features = extract_features(rows, interval="5m", start_ts=start, end_ts=start + 300)
    assert features.twap_initial == 100.0
    assert features.final_twap == 103.0
    assert features.open_move == 0.6  # 60s * 0.01
    assert set(features.snapshots) == set(checkpoints_for("5m"))
    snap = features.snapshots[30]
    assert snap["twap_diff"] == 2.7
    assert snap["fair_yes"] is not None and snap["fair_yes"] > 90
    assert features.realized_vol is not None


def test_extract_features_skips_checkpoints_without_data():
    start = 1_800_000_000
    rows = _window_rows(start, 300, base=100.0, drift=0.0)[:200]  # collector died at 200s
    features = extract_features(rows, interval="5m", start_ts=start, end_ts=start + 300)
    assert 30 not in features.snapshots
    assert features.final_twap is None
    assert 240 in features.snapshots
