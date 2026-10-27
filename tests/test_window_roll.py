from pmanalysis.collector.window_manager import WindowManager
from pmanalysis.db.models import Market, Window
from pmanalysis.db.session import get_session, init_db
from pmanalysis.feeds.gamma import DiscoveredWindow, GammaClient
from pmanalysis.feeds.slugs import boundary_ts, build_market_slug, next_boundary_ts


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()


def test_next_boundary_ts():
    assert next_boundary_ts(1780338600, "5m") == 1780338900


def test_roll_to_clock_creates_stub_and_swaps(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    now_ts = 1_780_338_601  # 1s into a 5m bucket starting 1780338600
    prev_start = boundary_ts(now_ts, "5m") - 300
    mgr = WindowManager(GammaClient())
    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        prev = Window(
            market_id=market.id,
            slug=build_market_slug("BTC", "5m", prev_start),
            asset="BTC",
            interval="5m",
            start_ts=prev_start,
            end_ts=prev_start + 300,
            yes_token_id="old-yes",
            no_token_id="old-no",
        )
        session.add(prev)
        session.flush()
        mgr._active_ids["BTC:5m"] = prev.id
        session.commit()
        prev_id = prev.id

    rolled = []
    with get_session() as session:
        rolled = mgr.roll_to_clock(session, now_ts)

    assert rolled
    assert rolled[0][0] == "BTC"
    assert rolled[0][2] == prev_id
    expected_slug = build_market_slug("BTC", "5m", now_ts)
    with get_session() as session:
        live = session.query(Window).filter_by(id=mgr._active_ids["BTC:5m"]).first()
        assert live.slug == expected_slug
        assert live.start_ts == boundary_ts(now_ts, "5m")


def test_sync_discovered_does_not_activate_next_early(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    now_ts = 1_780_338_601
    cur_start = boundary_ts(now_ts, "5m")
    nxt_start = cur_start + 300
    mgr = WindowManager(GammaClient())
    current = DiscoveredWindow(
        asset="BTC",
        interval="5m",
        base_slug="btc-updown-5m",
        slug=build_market_slug("BTC", "5m", cur_start),
        condition_id="c",
        yes_token_id="yes-now",
        no_token_id="no-now",
        question="now",
        strike_label="$1",
        start_ts=cur_start,
        end_ts=cur_start + 300,
        secs_left=200,
        role="current",
    )
    nxt = DiscoveredWindow(
        asset="BTC",
        interval="5m",
        base_slug="btc-updown-5m",
        slug=build_market_slug("BTC", "5m", nxt_start),
        condition_id="n",
        yes_token_id="yes-next",
        no_token_id="no-next",
        question="next",
        strike_label="$1",
        start_ts=nxt_start,
        end_ts=nxt_start + 300,
        secs_left=500,
        role="next",
    )
    with get_session() as session:
        tokens, rolled = mgr.sync_discovered(session, [current, nxt], now_ts=now_ts)
    assert not rolled
    assert mgr._active_ids["BTC:5m"]
    assert mgr._next_ids["BTC:5m"]
    assert mgr._active_ids["BTC:5m"] != mgr._next_ids["BTC:5m"]
    assert "yes-now" in tokens and "yes-next" not in tokens
    near_end = cur_start + 300 - 30
    with get_session() as session:
        overlap = mgr.subscription_token_ids(session, near_end)
        live = session.query(Window).filter_by(id=mgr._active_ids["BTC:5m"]).first()
        assert live.start_ts == cur_start
    assert "yes-now" in overlap and "yes-next" in overlap


def test_gamma_fill_does_not_move_clock_bounds(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    now_ts = 1_780_338_601
    cur_start = boundary_ts(now_ts, "5m")
    mgr = WindowManager(GammaClient())
    with get_session() as session:
        mgr.roll_to_clock(session, now_ts)
        stub_id = mgr._active_ids["BTC:5m"]

    filled = DiscoveredWindow(
        asset="BTC",
        interval="5m",
        base_slug="btc-updown-5m",
        slug=build_market_slug("BTC", "5m", now_ts),
        condition_id="c",
        yes_token_id="yes-now",
        no_token_id="no-now",
        question="now",
        strike_label="$1",
        start_ts=cur_start - 12,
        end_ts=cur_start + 288,
        secs_left=200,
        role="current",
    )
    with get_session() as session:
        tokens, rolled = mgr.sync_discovered(session, [filled], now_ts=now_ts)
        live = session.query(Window).filter_by(id=mgr._active_ids["BTC:5m"]).first()
        assert not rolled
        assert mgr._active_ids["BTC:5m"] == stub_id
        assert live.start_ts == cur_start
        assert live.end_ts == cur_start + 300
        assert live.yes_token_id == "yes-now"
        assert "yes-now" in tokens
