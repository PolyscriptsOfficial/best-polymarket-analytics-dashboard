from datetime import datetime, timezone

import pandas as pd

from pmanalysis.db.models import Market, Window
from pmanalysis.db.repository import (
    list_recent_previews,
    window_preview_payload,
    window_ticks_payload,
)
from pmanalysis.feeds.slugs import boundary_ts, build_market_slug
from pmanalysis.db.session import get_session, init_db
from pmanalysis.export.parquet import archive_path_for


def _reset_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", db_path)
    monkeypatch.setattr("pmanalysis.db.session._engine", None)
    monkeypatch.setattr("pmanalysis.db.session._SessionLocal", None)
    monkeypatch.setattr("pmanalysis.config.ARCHIVE_DIR", archive)
    init_db()
    return archive


def test_window_ticks_payload_falls_back_to_parquet(tmp_path, monkeypatch):
    _reset_db(tmp_path, monkeypatch)

    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        window = Window(
            market_id=market.id,
            slug="btc-updown-5m-parquet-fallback",
            asset="BTC",
            interval="5m",
            start_ts=1_000_000,
            end_ts=1_000_300,
            strike_price=100.0,
            opening_oracle_price=100.0,
            outcome="UP",
            archived=True,
            resolved_at=datetime.now(timezone.utc),
        )
        session.add(window)

    path = archive_path_for("BTC", "5m", "btc-updown-5m-parquet-fallback")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "ts_ms": 1_000_000_000,
                "oracle_price": 100.1,
                "binance_price": 100.2,
                "yes_mid": 0.55,
                "no_mid": 0.45,
                "yes_ask": 56.0,
                "no_ask": 46.0,
                "combined_ask": 102.0,
                "dist_from_strike": 0.1,
                "arb_edge": 1.0,
                "twap_oracle": 100.05,
            }
        ]
    ).to_parquet(path, index=False)

    with get_session() as session:
        payload = window_ticks_payload(session, "btc-updown-5m-parquet-fallback")
        preview = window_preview_payload(session, "btc-updown-5m-parquet-fallback")

    assert payload is not None
    assert len(payload["ticks"]) == 1
    assert payload["ticks"][0]["yes_mid"] == 0.55
    assert payload["ticks"][0]["oracle_price"] == 100.1
    assert preview is not None
    assert preview["tick_count"] == 1
    assert preview["ticks"][0]["yes_ask"] == 56.0


def test_recent_previews_hide_next_window(tmp_path, monkeypatch):
    _reset_db(tmp_path, monkeypatch)
    now_ts = 1_780_338_601
    cur_start = boundary_ts(now_ts, "5m")
    nxt_start = cur_start + 300
    with get_session() as session:
        market = session.query(Market).filter_by(base_slug="btc-updown-5m").first()
        session.add(
            Window(
                market_id=market.id,
                slug=build_market_slug("BTC", "5m", cur_start),
                asset="BTC",
                interval="5m",
                start_ts=cur_start,
                end_ts=cur_start + 300,
            )
        )
        session.add(
            Window(
                market_id=market.id,
                slug=build_market_slug("BTC", "5m", nxt_start),
                asset="BTC",
                interval="5m",
                start_ts=nxt_start,
                end_ts=nxt_start + 300,
            )
        )
        session.commit()
        payload = list_recent_previews(
            session, asset="BTC", interval="5m", now_ts=now_ts
        )
    slugs = [item["window"]["slug"] for item in payload["items"]]
    assert build_market_slug("BTC", "5m", cur_start) in slugs
    assert build_market_slug("BTC", "5m", nxt_start) not in slugs
