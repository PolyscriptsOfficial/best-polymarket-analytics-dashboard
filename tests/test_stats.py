from pmanalysis.db.models import Window
from pmanalysis.db.repository import stats_for_market, stats_win_rates
from pmanalysis.db.session import get_session, init_db


def _isolate_db(tmp_path, monkeypatch):
    db = tmp_path / "stats.db"
    monkeypatch.setattr("pmanalysis.config.DB_PATH", db)
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", db)
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()


def test_stats_win_rates_group_by(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    with get_session() as session:
        from pmanalysis.db.models import Market

        market = session.query(Market).filter_by(base_slug="btc-updown-5m").one()
        session.add_all(
            [
                Window(
                    market_id=market.id,
                    slug="btc-up-1",
                    asset="BTC",
                    interval="5m",
                    start_ts=1,
                    end_ts=2,
                    outcome="UP",
                ),
                Window(
                    market_id=market.id,
                    slug="btc-up-2",
                    asset="BTC",
                    interval="5m",
                    start_ts=3,
                    end_ts=4,
                    outcome="UP",
                ),
                Window(
                    market_id=market.id,
                    slug="btc-down-1",
                    asset="BTC",
                    interval="5m",
                    start_ts=5,
                    end_ts=6,
                    outcome="DOWN",
                ),
            ]
        )

    with get_session() as session:
        btc5 = stats_for_market(session, "BTC", "5m")
        assert btc5 is not None
        assert btc5["resolved"] == 3
        assert btc5["up"] == 2
        assert btc5["down"] == 1
        assert btc5["win_rate_up_pct"] == 66.7

        all_rows = stats_win_rates(session)
        assert len(all_rows) == 6
        eth5 = next(r for r in all_rows if r["asset"] == "ETH" and r["interval"] == "5m")
        assert eth5["resolved"] == 0
        assert eth5["win_rate_up_pct"] is None
