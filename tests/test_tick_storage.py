"""Slim tick storage: derived fields on read, one-time legacy migration."""

import sqlite3

from pmanalysis.db.models import Market, Tick, Window
from pmanalysis.db.session import get_session, init_db
from pmanalysis.db.tick_fields import STORED_TICK_COLUMNS, derive_tick_fields, with_derived


def test_derive_tick_fields_matches_live_formulas():
    row = {
        "ts_ms": 1_000_060_000,
        "yes_ask": 52.0,
        "yes_bid": 50.0,
        "no_ask": 49.0,
        "no_bid": 47.0,
        "oracle_price": 97450.0,
        "twap_oracle": 97440.5,
    }
    f = derive_tick_fields(row, end_ts=1_000_300, strike_price=97432.0, twap_initial=97430.0)
    assert (f["yes_mid"], f["no_mid"]) == (0.51, 0.48)
    assert f["combined_ask"] == 101.0 and f["arb_edge"] == -1.0
    assert f["yes_spread"] == 2.0 and f["lean"] == 0.03
    assert f["secs_remaining"] == 240
    assert f["dist_from_strike"] == 18.0 and f["twap_diff"] == 10.5


def test_with_derived_keeps_values_from_old_archives():
    old = {"ts_ms": 0, "yes_ask": 52.0, "yes_bid": 50.0, "yes_mid": 0.5123, "twap_diff": 1.0}
    (row,) = with_derived([old], end_ts=300, strike_price=None, twap_initial=None)
    assert row["yes_mid"] == 0.5123 and row["twap_diff"] == 1.0  # stored copies win
    assert row["combined_ask"] == 52.0 and row["secs_remaining"] == 300


def _legacy_db(path):
    """Pre-2026-10-31 layout: fat ticks + a parallel indicators table."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE markets (id INTEGER PRIMARY KEY, asset VARCHAR(8) NOT NULL,
            interval VARCHAR(8) NOT NULL, base_slug VARCHAR(64) NOT NULL UNIQUE);
        CREATE TABLE ticks (id INTEGER PRIMARY KEY, window_id INTEGER NOT NULL,
            ts_ms BIGINT NOT NULL, oracle_price FLOAT, binance_price FLOAT,
            binance_perp_price FLOAT, yes_ask FLOAT NOT NULL, yes_bid FLOAT NOT NULL,
            no_ask FLOAT NOT NULL, no_bid FLOAT NOT NULL, yes_mid FLOAT NOT NULL,
            no_mid FLOAT NOT NULL, combined_ask FLOAT NOT NULL, yes_spread FLOAT NOT NULL,
            secs_remaining INTEGER NOT NULL, dist_from_strike FLOAT, yes_ask_size FLOAT);
        CREATE TABLE indicators (id INTEGER PRIMARY KEY, window_id INTEGER NOT NULL,
            ts_ms BIGINT NOT NULL, implied_yes_prob FLOAT NOT NULL, arb_edge FLOAT NOT NULL,
            lean FLOAT NOT NULL, momentum_1m FLOAT, volatility_5m FLOAT,
            time_decay_factor FLOAT NOT NULL, twap_oracle FLOAT, fair_yes FLOAT);
        INSERT INTO ticks VALUES (1, 7, 1000, 100.0, 100.1, NULL, 52, 50, 49, 47,
            0.51, 0.48, 101, 2, 299, 0.5, 120);
        INSERT INTO ticks VALUES (2, 7, 2000, 100.2, 100.3, NULL, 53, 51, 48, 46,
            0.52, 0.47, 101, 2, 298, 0.7, NULL);
        INSERT INTO indicators VALUES (1, 7, 1000, 0.51, -1, 0.03, NULL, NULL, 0.1, 99.9, 55.5);
        -- duplicate indicator row for the same second must not duplicate the tick
        INSERT INTO indicators VALUES (2, 7, 1000, 0.51, -1, 0.03, NULL, NULL, 0.1, 99.9, 55.5);
        """
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_legacy_ticks(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _legacy_db(db_path)
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", db_path)
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    init_db()

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(ticks)")]
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "indicators" not in tables and "ticks_legacy" not in tables
    assert columns == ["id", "window_id", "ts_ms", *STORED_TICK_COLUMNS]
    assert "ix_ticks_window_ts" in indexes

    with get_session() as session:
        rows = session.query(Tick).order_by(Tick.ts_ms).all()
        assert [(r.id, r.ts_ms) for r in rows] == [(1, 1000), (2, 2000)]
        assert (rows[0].twap_oracle, rows[0].fair_yes, rows[0].yes_ask_size) == (99.9, 55.5, 120)
        assert rows[1].twap_oracle is None and rows[1].yes_bid == 51

    init_db()  # idempotent: a second start leaves the slim table alone


def test_sampler_row_matches_model():
    """Every stored column has a model attribute (guards against drift)."""
    assert set(STORED_TICK_COLUMNS) <= set(Tick.__table__.columns.keys())
    assert {"yes_mid", "secs_remaining"}.isdisjoint(Tick.__table__.columns.keys())
    assert Market and Window  # imported for metadata registration
