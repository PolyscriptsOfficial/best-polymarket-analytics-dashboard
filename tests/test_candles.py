from pmanalysis.candles import (
    CandleBook,
    Ohlc,
    apply_price,
    direction,
    list_window_candles,
    persist_candles,
    seal_window_candle,
)
from pmanalysis.db.models import Market, Window
from pmanalysis.db.session import get_session, init_db
from pmanalysis.feeds.slugs import boundary_ts, build_market_slug
from pmanalysis.web.app import create_app
from fastapi.testclient import TestClient


def test_apply_price_opens_then_tracks_range():
    bar = apply_price(None, 100.0)
    assert bar == Ohlc(open=100.0, high=100.0, low=100.0, close=100.0)
    bar = apply_price(bar, 104.0)
    bar = apply_price(bar, 97.0)
    assert bar.open == 100.0
    assert bar.high == 104.0
    assert bar.low == 97.0
    assert bar.close == 97.0


def test_apply_price_pins_official_open():
    bar = apply_price(None, 101.0)
    bar = apply_price(bar, 103.0, open_price=100.0)
    assert bar.open == 100.0
    assert bar.high == 103.0
    assert bar.low == 100.0


def test_direction_uses_outcome_then_close():
    assert direction(100, 101) == "UP"
    assert direction(100, 99) == "DOWN"
    assert direction(100, 101, "DOWN") == "DOWN"


def test_candle_book_rolls_to_new_window():
    book = CandleBook()
    book.update("BTC", "5m", 1000, 10.0, 10.0)
    book.update("BTC", "5m", 1000, 12.0)
    book.update("BTC", "5m", 1300, 11.0, 11.0)
    snap = book.snapshot("BTC", "5m")
    assert snap["time"] == 1300
    assert snap["open"] == 11.0
    assert snap["close"] == 11.0
    dirty = book.take_dirty()
    assert len(dirty) == 1
    assert dirty[0][2] == 1300


def test_list_window_candles_from_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()
    start = boundary_ts(1_780_338_601, "5m")
    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        session.add(
            Window(
                market_id=market.id,
                slug=build_market_slug("BTC", "5m", start),
                asset="BTC",
                interval="5m",
                start_ts=start,
                end_ts=start + 300,
                opening_oracle_price=100.0,
                final_oracle_price=101.5,
                strike_price=100.0,
                outcome="UP",
            )
        )
        session.commit()
        persist_candles(
            session,
            [("BTC", "5m", start, Ohlc(open=100.0, high=102.0, low=99.5, close=101.5))],
        )

    with get_session() as session:
        payload = list_window_candles(session, "BTC", "5m")
    assert payload["step_sec"] == 300
    assert len(payload["items"]) == 1
    bar = payload["items"][0]
    assert bar["open"] == 100.0
    assert bar["high"] == 102.0
    assert bar["outcome"] == "UP"


def test_seal_keeps_recorded_open(tmp_path, monkeypatch):
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", tmp_path / "seal.db")
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()
    start = boundary_ts(1_780_338_601, "5m")
    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        window = Window(
            market_id=market.id,
            slug=build_market_slug("BTC", "5m", start),
            asset="BTC",
            interval="5m",
            start_ts=start,
            end_ts=start + 300,
            opening_oracle_price=100.0,
            final_oracle_price=99.0,
            strike_price=100.0,
            outcome="DOWN",
        )
        session.add(window)
        session.commit()
        persist_candles(
            session,
            [("BTC", "5m", start, Ohlc(open=101.25, high=103.0, low=98.5, close=100.5))],
        )
        session.refresh(window)
        seal_window_candle(session, window)
        session.commit()

    with get_session() as session:
        payload = list_window_candles(session, "BTC", "5m")
    bar = payload["items"][0]
    assert bar["open"] == 101.25
    assert bar["close"] == 99.0
    assert bar["high"] == 103.0
    assert bar["low"] == 98.5


def test_api_candles_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", tmp_path / "web.db")
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()
    client = TestClient(create_app())
    res = client.get("/api/candles/BTC/5m")
    assert res.status_code == 200
    body = res.json()
    assert body["asset"] == "BTC"
    assert body["interval"] == "5m"
    assert body["step_sec"] == 300
    assert "items" in body
    page = client.get("/")
    assert "chart-candles" in page.text
    assert "5m candles" in page.text
