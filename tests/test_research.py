import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pmanalysis.analytics.research import BacktestSpec, calibration, regime_stats, run_backtest
from pmanalysis.collector.resolution import (
    apply_resolution,
    infer_outcome,
    mismatch_summary,
    refresh_inference,
)
from pmanalysis.db.models import Base, Market, Window, WindowSnapshot


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Market(id=1, asset="BTC", interval="5m", base_slug="btc-updown-5m"))
        s.commit()
        yield s


def _add_window(s, i, start, outcome, *, yes_ask, no_ask, diff, fair=None, vol=None):
    w = Window(
        id=i,
        market_id=1,
        slug=f"btc-updown-5m-{start}",
        asset="BTC",
        interval="5m",
        start_ts=start,
        end_ts=start + 300,
        outcome=outcome,
        realized_vol=vol,
        open_move=diff,
    )
    s.add(w)
    s.add(
        WindowSnapshot(
            window_id=i,
            asset="BTC",
            interval="5m",
            start_ts=start,
            t_minus=30,
            yes_ask=yes_ask,
            yes_bid=yes_ask - 2,
            no_ask=no_ask,
            no_bid=no_ask - 2,
            twap_diff=diff,
            fair_yes=fair,
        )
    )


def test_backtest_auto_side_pnl(session):
    base = (int(time.time()) // 300) * 300 - 300 * 10
    _add_window(session, 1, base, "UP", yes_ask=80, no_ask=22, diff=5.0, fair=90)
    _add_window(session, 2, base + 300, "UP", yes_ask=30, no_ask=72, diff=-3.0, fair=20)
    _add_window(session, 3, base + 600, "DOWN", yes_ask=85, no_ask=17, diff=2.0, fair=70)
    session.commit()

    result = run_backtest(
        session, BacktestSpec(asset="BTC", interval="5m", t_minus=30, days=1, max_ask=90)
    )
    s = result["summary"]
    # W1: YES @80 wins (+20¢); W2: NO @72 loses (-72¢); W3: YES @85 loses (-85¢).
    assert s["trades"] == 3
    assert s["wins"] == 1
    assert s["pnl_usd"] == pytest.approx((20 - 72 - 85) / 100 * 10)
    assert s["max_drawdown_usd"] == pytest.approx(-15.7)

    # Edge filter: W1 YES 90-80=10¢ passes; W2 NO (100-20)-72=8¢ and W3 YES 70-85 fail.
    filtered = run_backtest(
        session,
        BacktestSpec(asset="BTC", interval="5m", t_minus=30, days=1, max_ask=90, min_edge=9),
    )
    assert filtered["summary"]["trades"] == 1
    assert filtered["summary"]["skipped"]["edge"] == 2


def test_calibration_and_regimes(session):
    base = (int(time.time()) // 300) * 300 - 300 * 10
    for i in range(6):
        _add_window(
            session, i + 1, base + i * 300, "UP" if i % 2 == 0 else "DOWN",
            yes_ask=95 if i % 2 == 0 else 6, no_ask=6, diff=1.0, fair=90, vol=0.1 * (i + 1),
        )
    session.commit()
    cal = calibration(session, asset="BTC", interval="5m", t_minus=30, days=1)
    assert cal["market"]["n"] == 6
    top = [b for b in cal["market"]["bins"] if b["lo"] == 90][0]
    assert top["n"] == 3 and top["up_rate"] == 100.0

    reg = regime_stats(session, asset="BTC", interval="5m", days=1)
    assert reg["overall"]["n"] == 6
    after_up = [r for r in reg["after_streak"] if r["side"] == "UP" and r["k"] == 1][0]
    assert after_up["n"] == 3 and after_up["up_rate"] == 0.0  # strict alternation
    assert len(reg["by_volatility"]) == 3


def test_resolution_inference_and_mismatch(session):
    w = Window(
        id=10, market_id=1, slug="btc-updown-5m-1", asset="BTC", interval="5m",
        start_ts=0, end_ts=300, twap_initial=100.0, final_twap=100.0,
    )
    session.add(w)
    assert infer_outcome(w) == "UP"  # ties resolve UP
    apply_resolution(w, None)
    assert (w.outcome, w.outcome_source, w.outcome_mismatch) == ("UP", "inferred", False)
    apply_resolution(w, "DOWN")
    assert (w.outcome, w.outcome_source, w.outcome_mismatch) == ("DOWN", "gamma", True)
    session.commit()
    summary = mismatch_summary(session)
    assert summary["mismatches"] == 1
    assert summary["recent_mismatches"][0]["inferred_outcome"] == "UP"


def test_spot_fallback_never_flags_mismatch(session):
    w = Window(
        id=11, market_id=1, slug="btc-updown-5m-2", asset="BTC", interval="5m",
        start_ts=0, end_ts=300, strike_price=100.0, final_oracle_price=99.0, final_twap=101.0,
    )
    session.add(w)
    apply_resolution(w, "UP")
    assert w.inferred_outcome == "DOWN"  # spot guess, no TWAP target
    assert w.outcome_mismatch is False


def test_refresh_inference_after_twap_recovered(session):
    # Finalized before the TWAP fields existed: spot guess, no mismatch possible.
    w = Window(
        id=12, market_id=1, slug="btc-updown-5m-3", asset="BTC", interval="5m",
        start_ts=0, end_ts=300, strike_price=100.0, final_oracle_price=99.0,
    )
    apply_resolution(w, "UP")
    assert (w.inferred_outcome, w.outcome_mismatch) == ("DOWN", False)
    # Feature extraction later recovers both TWAPs from the 1s rows.
    w.twap_initial, w.final_twap = 100.0, 99.5
    assert refresh_inference(w) is False  # Gamma's outcome stands
    assert (w.inferred_outcome, w.outcome_mismatch) == ("DOWN", True)

    inferred = Window(
        id=13, market_id=1, slug="btc-updown-5m-4", asset="BTC", interval="5m",
        start_ts=0, end_ts=300, strike_price=100.0, final_oracle_price=99.0,
    )
    apply_resolution(inferred, None)
    assert (inferred.outcome, inferred.outcome_source) == ("DOWN", "inferred")
    inferred.twap_initial, inferred.final_twap = 100.0, 100.4
    assert refresh_inference(inferred) is True  # TWAP inference replaces the spot guess
    assert inferred.outcome == "UP"

    legacy = Window(
        id=14, market_id=1, slug="btc-updown-5m-5", asset="BTC", interval="5m",
        start_ts=0, end_ts=300, outcome="UP", twap_initial=100.0, final_twap=99.0,
    )
    assert refresh_inference(legacy) is False  # unknown origin: untouched
    assert legacy.inferred_outcome is None and legacy.outcome == "UP"
