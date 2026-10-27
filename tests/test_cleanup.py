from datetime import datetime, timedelta, timezone

from pmanalysis.db.cleanup import compact_live_db, purge_archived_ticks, purge_raw_feeds, remove_impossible_quote_ticks
from pmanalysis.db.models import BinanceTrade, Market, Tick, Window
from pmanalysis.db.session import get_session, init_db


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()


def test_remove_impossible_quote_ticks(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    with get_session() as session:
        market_row = session.query(Market).filter_by(base_slug="sol-updown-5m").first()
        window = Window(
            market_id=market_row.id,
            slug="sol-updown-5m-test",
            asset="SOL",
            interval="5m",
            start_ts=1,
            end_ts=2,
        )
        session.add(window)
        session.flush()
        window_id = window.id
        session.add_all([
            Tick(window_id=window_id, ts_ms=1000, yes_ask=51.0, no_ask=50.0),
            Tick(window_id=window_id, ts_ms=2000, yes_ask=91.0, no_ask=91.0),
            Tick(window_id=window_id, ts_ms=3000, yes_ask=10.0, no_ask=92.0),
        ])
        session.commit()

    result = remove_impossible_quote_ticks()
    assert result["ticks_removed"] == 1
    assert result["windows_affected"] == 1

    with get_session() as session:
        ticks = session.query(Tick).filter_by(window_id=window_id).order_by(Tick.ts_ms).all()
        assert len(ticks) == 2
        assert all(not (t.yes_ask >= 90 and t.no_ask >= 90) for t in ticks)


def test_purge_archived_ticks_keeps_recent_and_catalog(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        window = Window(
            market_id=market.id,
            slug="btc-updown-5m-old",
            asset="BTC",
            interval="5m",
            start_ts=1,
            end_ts=2,
            archived=True,
            resolved_at=old,
        )
        session.add(window)
        session.flush()
        session.add(Tick(window_id=window.id, ts_ms=1000, yes_mid=0.5, no_mid=0.5))
        session.commit()
        window_id = window.id

    result = purge_archived_ticks(retention_hours=24)
    assert result["ticks_removed"] == 1
    assert result["retention_hours"] == 24

    with get_session() as session:
        assert session.query(Window).filter_by(id=window_id).first() is not None
        assert session.query(Tick).filter_by(window_id=window_id).count() == 0


def test_purge_raw_feeds_drops_tables_when_feed_record_off(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    monkeypatch.setattr("pmanalysis.db.cleanup.FEED_RECORD_ENABLED", False)
    with get_session() as session:
        session.add(BinanceTrade(asset="BTC", ts_ms=1, price=1.0))
        session.commit()

    result = purge_raw_feeds(rebuild=True)
    assert result["feed_record"] is False
    assert result["binance_trades"] >= 1

    with get_session() as session:
        assert session.query(BinanceTrade).count() == 0


def test_compact_live_db_drops_raw_feeds_keeps_recent_ticks(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", db_path)

    old = datetime.now(timezone.utc) - timedelta(hours=48)
    fresh = datetime.now(timezone.utc)
    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        old_w = Window(
            market_id=market.id,
            slug="btc-old",
            asset="BTC",
            interval="5m",
            start_ts=1,
            end_ts=2,
            archived=True,
            resolved_at=old,
        )
        new_w = Window(
            market_id=market.id,
            slug="btc-new",
            asset="BTC",
            interval="5m",
            start_ts=3,
            end_ts=4,
            archived=True,
            resolved_at=fresh,
        )
        session.add_all([old_w, new_w])
        session.flush()
        session.add(Tick(window_id=old_w.id, ts_ms=1, yes_mid=0.1, no_mid=0.9))
        session.add(Tick(window_id=new_w.id, ts_ms=2, yes_mid=0.6, no_mid=0.4))
        session.add(BinanceTrade(asset="BTC", ts_ms=1, price=1.0))
        session.commit()

    result = compact_live_db()
    assert result["ok"] is True
    assert result["ticks"] == 1

    with get_session() as session:
        assert session.query(Window).count() >= 2
        assert session.query(Tick).count() == 1
        assert session.query(Tick).first().ts_ms == 2
        assert session.query(BinanceTrade).count() == 0
