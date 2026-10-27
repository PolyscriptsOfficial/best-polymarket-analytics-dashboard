from pmanalysis.collector.gaps import GapTracker
from pmanalysis.collector.health import classify_feed, is_gap_status
from pmanalysis.db.models import CollectorGap
from pmanalysis.db.repository import list_collector_gaps
from pmanalysis.db.session import get_session, init_db


def _isolate_db(tmp_path, monkeypatch):
    db = tmp_path / "gaps.db"
    monkeypatch.setattr("pmanalysis.config.DB_PATH", db)
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", db)
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()
    return db


def test_classify_feed_states():
    assert classify_feed(connected=True, lag_sec=1.2, threshold_sec=15) == "ok"
    assert classify_feed(connected=True, lag_sec=20, threshold_sec=15) == "stale"
    assert classify_feed(connected=False, lag_sec=1.2, threshold_sec=15) == "down"
    assert classify_feed(connected=True, lag_sec=None, threshold_sec=15) == "waiting"
    assert classify_feed(connected=True, lag_sec=1.2, threshold_sec=15, idle=True) == "idle"
    assert is_gap_status("stale")
    assert is_gap_status("down")
    assert not is_gap_status("ok")
    assert not is_gap_status("idle")


def test_gap_tracker_opens_and_closes(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    tracker = GapTracker()
    down = {
        "source": "binance",
        "status": "down",
        "lag_sec": None,
        "threshold_sec": 15,
    }
    ok = {**down, "status": "ok", "lag_sec": 0.4}

    with get_session() as session:
        tracker.hydrate(session)
        tracker.reconcile(session, [down])
        open_rows = list_collector_gaps(session, open_only=True)
        assert len(open_rows) == 1
        assert open_rows[0]["source"] == "binance"
        assert open_rows[0]["open"] is True

        tracker.reconcile(session, [down])
        assert session.query(CollectorGap).count() == 1

        tracker.reconcile(session, [ok])
        open_rows = list_collector_gaps(session, open_only=True)
        assert open_rows == []
        recent = list_collector_gaps(session, open_only=False, limit=5)
        assert len(recent) == 1
        assert recent[0]["open"] is False
        assert recent[0]["ended_at"] is not None
