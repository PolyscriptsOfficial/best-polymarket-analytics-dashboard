from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from pmanalysis import config
from pmanalysis.datasets.builder import build_dataset_zip
from pmanalysis.datasets.client import DatasetClientError, fetch_dataset, load_dataset_zip
from pmanalysis.datasets.completeness import evaluate_completeness
from pmanalysis.datasets.ids import zip_filename
from pmanalysis.datasets.query import clear_archive_fingerprint_cache, fingerprint_revision
from pmanalysis.datasets.schema import SpecError, parse_spec
from pmanalysis.datasets.service import DatasetService, DatasetStore
from pmanalysis.datasets.store import DatasetRecord
from pmanalysis.datasets.ticks import mid_to_cents
from pmanalysis.db.models import Market, Tick, Window
from pmanalysis.db.session import get_session, init_db, reset_engine
from pmanalysis.export.parquet import archive_path_for
from pmanalysis.web.app import create_app

PERIOD_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2024, 1, 2, tzinfo=timezone.utc)
WINDOW_START = int(datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _historic_fixtures_allowed(monkeypatch):
    """Fixtures are dated 2024; the public 30-day window is tested separately."""
    monkeypatch.setattr(config, "DATASET_LOOKBACK_DAYS", 100_000)
    monkeypatch.setattr(config, "DATASET_MAX_RANGE_DAYS", 366)


def _isolate(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "test.db"
    archive = tmp_path / "archive"
    exports = tmp_path / "exports"
    datasets = exports / "datasets"
    archive.mkdir()
    datasets.mkdir(parents=True)
    monkeypatch.setattr("pmanalysis.db.session.DB_PATH", db)
    monkeypatch.setattr("pmanalysis.config.ARCHIVE_DIR", archive)
    monkeypatch.setattr("pmanalysis.config.EXPORT_DIR", exports)
    monkeypatch.setattr("pmanalysis.config.DATASET_DIR", datasets)
    clear_archive_fingerprint_cache()
    reset_engine()
    init_db()
    return datasets


def _spec(**kwargs):
    body = {
        "start": PERIOD_START,
        "end": PERIOD_END,
        "assets": ["BTC"],
        "intervals": ["5m"],
    }
    body.update(kwargs)
    return parse_spec(**body)


def _market(session, asset: str = "BTC", interval: str = "5m") -> Market:
    return session.query(Market).filter_by(
        base_slug=f"{asset.lower()}-updown-{interval}"
    ).first()


def _add_window(
    session,
    *,
    asset: str = "BTC",
    interval: str = "5m",
    start_ts: int = WINDOW_START,
    outcome: str | None = "UP",
    archived: bool = False,
    n_ticks: int = 300,
    yes_ask: float = 90.0,
    slug: str | None = None,
) -> Window:
    length = 300 if interval == "5m" else 900
    end_ts = start_ts + length
    window = Window(
        market_id=_market(session, asset, interval).id,
        slug=slug or f"{asset.lower()}-updown-{interval}-{start_ts}",
        asset=asset,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
        strike_price=100.0,
        opening_oracle_price=100.0,
        final_oracle_price=100.4,
        outcome=outcome,
        archived=archived,
        resolved_at=datetime(2024, 1, 1, 12, 10, tzinfo=timezone.utc) if outcome else None,
    )
    session.add(window)
    session.flush()
    if not archived:
        for i in range(n_ticks):
            session.add(
                Tick(
                    window_id=window.id,
                    ts_ms=(start_ts + i) * 1000,
                    oracle_price=100.0,
                    binance_price=100.1,
                    yes_bid=yes_ask - 1,
                    yes_ask=yes_ask,
                    no_bid=9.0,
                    no_ask=10.0,
                )
            )
    return window


def _write_archive(window: Window, n_ticks: int = 300, yes_ask: float = 90.0) -> None:
    length = window.end_ts - window.start_ts
    rows = []
    for i in range(n_ticks):
        rows.append(
            {
                "ts_ms": (window.start_ts + i) * 1000,
                "secs_remaining": max(length - i, 0),
                "oracle_price": 100.0,
                "binance_price": 100.1,
                "twap_oracle": 100.05,
                "yes_bid": yes_ask - 1,
                "yes_ask": yes_ask,
                "no_bid": 9.0,
                "no_ask": 10.0,
                "yes_mid": 0.895,
                "no_mid": 0.095,
                "combined_ask": yes_ask + 10.0,
                "yes_spread": 1.0,
                "dist_from_strike": 0.1,
            }
        )
    path = archive_path_for(window.asset, window.interval, window.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def _wait_ready(service: DatasetService, dataset_id: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = service.store.get(dataset_id)
        assert record is not None
        if record.status == "ready":
            return record
        if record.status == "failed":
            raise AssertionError(record.error)
        time.sleep(0.05)
    raise AssertionError("dataset did not become ready")


def test_mid_to_cents():
    assert mid_to_cents(0.895) == 89.5
    assert mid_to_cents(90.0) == 90.0
    assert mid_to_cents(0.0) == 0.0
    assert mid_to_cents(None) is None


def test_archive_fingerprint_refresh_on_force(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        window = _add_window(session, archived=True, n_ticks=0)
        _write_archive(window, n_ticks=10)
        session.commit()
        spec = _spec()
        first = fingerprint_revision(session, spec)
        path = archive_path_for(window.asset, window.interval, window.slug)
        path.write_bytes(path.read_bytes() + b"xxxx")
        cached = fingerprint_revision(session, spec)
        assert cached == first
        fresh = fingerprint_revision(session, spec, refresh_archive=True)
        assert fresh != first


def test_parse_spec_canonical_and_rejects():
    spec = parse_spec(
        start=PERIOD_START,
        end=PERIOD_END,
        assets=["SOL", "BTC"],
        intervals=["15m", "5m"],
    )
    assert spec.assets == ("BTC", "SOL")
    assert spec.intervals == ("5m", "15m")
    try:
        parse_spec(start=PERIOD_START, end=PERIOD_START, assets=["BTC"], intervals=["5m"])
        assert False, "expected SpecError"
    except SpecError:
        pass
    try:
        parse_spec(start=PERIOD_START, end=PERIOD_END, assets=["DOGE"], intervals=["5m"])
        assert False, "expected SpecError"
    except SpecError:
        pass


def test_completeness_full_window():
    start = 1_000_000
    ts = [ (start + i) * 1000 for i in range(300) ]
    result = evaluate_completeness(ts, start_ts=start, end_ts=start + 300, interval="5m")
    assert result.tick_count == 300
    assert result.complete is True


def test_completeness_rejects_sparse_and_gappy():
    start = 1_000_000
    short = evaluate_completeness(
        [(start + i) * 1000 for i in range(10)],
        start_ts=start,
        end_ts=start + 300,
        interval="5m",
    )
    assert short.complete is False
    gappy = [(start + i) * 1000 for i in range(280)]
    gappy.append((start + 290) * 1000)
    gappy.extend((start + i) * 1000 for i in range(291, 300))
    result = evaluate_completeness(gappy, start_ts=start, end_ts=start + 300, interval="5m")
    assert result.complete is False


def test_builder_filters_market_and_live_window(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        btc = _add_window(session, n_ticks=300, yes_ask=90.0)
        _add_window(
            session,
            asset="ETH",
            start_ts=WINDOW_START + 300,
            n_ticks=300,
            slug="eth-updown-5m-extra",
        )
        _add_window(
            session,
            start_ts=WINDOW_START - 86_400,
            n_ticks=300,
            slug="btc-updown-5m-before",
        )
        live_start = int(datetime.now(timezone.utc).timestamp()) + 120
        _add_window(
            session,
            start_ts=live_start,
            n_ticks=0,
            outcome=None,
            slug=f"btc-updown-5m-{live_start}",
        )
        btc_id = btc.id

    spec = _spec()
    dest = datasets / "manual" / "dataset.zip"
    built = build_dataset_zip(spec, dest, dataset_id="ds_test", data_revision="abc")
    assert built.window_count == 1
    assert built.filename == "btc_5m_20240101_20240102.zip"
    manifest, windows, ticks = load_dataset_zip(
        dest,
        start="2024-01-01T00:00:00Z",
        end="2024-01-02T00:00:00Z",
        assets=["BTC"],
        intervals=["5m"],
    )
    assert manifest["window_count"] == 1
    assert list(windows["window_id"]) == [btc_id]
    assert set(ticks["window_id"].unique()) == {btc_id}
    assert float(ticks["yes_ask"].iloc[0]) == 90.0
    assert float(ticks["yes_mid"].iloc[0]) == 89.5
    tick_files = [item["path"] for item in manifest["files"] if item["path"].startswith("ticks/")]
    assert tick_files == ["ticks/asset=BTC/interval=5m/date=2024-01-01.parquet"]
    assert [item["path"] for item in manifest["files"]].count("windows.parquet") == 1


def test_builder_two_days_one_manifest(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    day2 = WINDOW_START + 86_400
    with get_session() as session:
        _add_window(session, start_ts=WINDOW_START, n_ticks=300)
        _add_window(session, start_ts=day2, n_ticks=300, slug=f"btc-updown-5m-{day2}")

    spec = parse_spec(
        start=PERIOD_START,
        end=datetime(2024, 1, 3, tzinfo=timezone.utc),
        assets=["BTC"],
        intervals=["5m"],
    )
    dest = datasets / "two-day" / "dataset.zip"
    built = build_dataset_zip(spec, dest, dataset_id="ds_two", data_revision="t2")
    assert built.window_count == 2
    manifest, windows, ticks = load_dataset_zip(dest)
    names = [item["path"] for item in manifest["files"]]
    assert names.count("windows.parquet") == 1
    assert names.count("manifest.json") == 0
    assert "ticks/asset=BTC/interval=5m/date=2024-01-01.parquet" in names
    assert "ticks/asset=BTC/interval=5m/date=2024-01-02.parquet" in names
    assert len(windows) == 2
    assert ticks["window_id"].nunique() == 2


def test_builder_reads_archived_parquet(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        window = _add_window(session, archived=True, n_ticks=0, yes_ask=90.0)
        _write_archive(window, n_ticks=300, yes_ask=90.0)

    spec = _spec()
    dest = datasets / "archive-case" / "dataset.zip"
    built = build_dataset_zip(spec, dest, dataset_id="ds_arch", data_revision="a1")
    assert built.window_count == 1
    _, windows, ticks = load_dataset_zip(dest)
    assert bool(windows.iloc[0]["complete"]) is True
    assert float(ticks["yes_ask"].iloc[0]) == 90.0
    assert float(ticks["yes_mid"].iloc[0]) == 89.5


def test_service_reuses_identical_request(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        _add_window(session)

    service = DatasetService(store=DatasetStore(directory=datasets))
    service.start()
    spec = _spec()
    first, created = service.submit(spec)
    assert created is True
    ready = _wait_ready(service, first.dataset_id)
    second, created_again = service.submit(spec)
    assert created_again is False
    assert second.dataset_id == ready.dataset_id
    assert second.status == "ready"
    service._pool.shutdown(wait=False, cancel_futures=True)


def test_v1_coverage_and_download(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        _add_window(session, n_ticks=300, yes_ask=90.0)

    app = create_app()
    with TestClient(app) as client:
        open_coverage = client.get("/v1/coverage")
        assert open_coverage.status_code == 200
        assert "markets" in open_coverage.json()

        coverage = client.get("/v1/coverage?asset=BTC&interval=5m")
        assert coverage.status_code == 200
        markets = coverage.json()["markets"]
        assert len(markets) == 1
        assert markets[0]["asset"] == "BTC"
        assert markets[0]["window_count"] == 1
        assert "archived_window_count" in markets[0]
        assert "complete_window_count" not in markets[0]

        empty_assets = client.post(
            "/v1/datasets",
            json={
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-02T00:00:00Z",
                "assets": [],
                "intervals": ["5m"],
            },
        )
        assert empty_assets.status_code == 400

        bad = client.post(
            "/v1/datasets",
            json={
                "start": "2024-01-02T00:00:00Z",
                "end": "2024-01-01T00:00:00Z",
                "assets": ["BTC"],
                "intervals": ["5m"],
            },
        )
        assert bad.status_code == 400

        created = client.post(
            "/v1/datasets",
            json={
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-02T00:00:00Z",
                "assets": ["BTC"],
                "intervals": ["5m"],
            },
        )
        assert created.status_code in (200, 202)
        dataset_id = created.json()["dataset_id"]
        assert dataset_id.startswith("ds_")

        deadline = time.time() + 15
        status_body = None
        while time.time() < deadline:
            status = client.get(f"/v1/datasets/{dataset_id}")
            assert status.status_code == 200
            status_body = status.json()
            if status_body["status"] == "ready":
                break
            if status_body["status"] == "failed":
                raise AssertionError(status_body.get("error"))
            time.sleep(0.05)
        assert status_body is not None
        assert status_body["status"] == "ready"
        assert status_body["window_count"] == 1
        assert status_body["assets"] == ["BTC"]
        assert status_body["intervals"] == ["5m"]

        download = client.get(f"/v1/datasets/{dataset_id}/download")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/zip")
        zip_path = tmp_path / "got.zip"
        zip_path.write_bytes(download.content)
        manifest, windows, ticks = load_dataset_zip(zip_path)
        assert manifest["assets"] == ["BTC"]
        assert zip_filename(_spec()) in download.headers.get("content-disposition", "")
        assert not windows.empty
        assert float(ticks["yes_ask"].iloc[0]) == 90.0

        again = client.post(
            "/v1/datasets",
            json={
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-02T00:00:00Z",
                "assets": ["BTC"],
                "intervals": ["5m"],
            },
        )
        assert again.status_code == 200
        assert again.json()["dataset_id"] == dataset_id
        assert again.json()["status"] == "ready"

        meta = tmp_path / "exports" / "datasets" / dataset_id / "meta.json"
        mtime = meta.stat().st_mtime_ns
        client.get(f"/v1/datasets/{dataset_id}")
        client.get(f"/v1/datasets/{dataset_id}")
        assert meta.stat().st_mtime_ns == mtime

        load_dataset_zip(
            zip_path,
            start="2024-01-01T00:00:00Z",
            end="2024-01-02T00:00:00Z",
            assets=["btc"],
            intervals=["5m"],
        )


def test_empty_range_zip(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    dest = datasets / "empty" / "dataset.zip"
    built = build_dataset_zip(_spec(), dest, dataset_id="ds_empty", data_revision="e0")
    assert built.window_count == 0
    manifest, windows, ticks = load_dataset_zip(dest)
    assert manifest["window_count"] == 0
    assert windows.empty
    assert ticks.empty


def test_builder_15m_only(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        _add_window(session, interval="15m", n_ticks=900)
        _add_window(session, interval="5m", n_ticks=300, slug="btc-updown-5m-keep-out")

    spec = parse_spec(
        start=PERIOD_START,
        end=PERIOD_END,
        assets=["BTC"],
        intervals=["15m"],
    )
    dest = datasets / "m15" / "dataset.zip"
    built = build_dataset_zip(spec, dest, dataset_id="ds_15", data_revision="15")
    assert built.window_count == 1
    manifest, windows, ticks = load_dataset_zip(dest, assets=["BTC"], intervals=["15m"])
    names = [item["path"] for item in manifest["files"] if item["path"].startswith("ticks/")]
    assert names == ["ticks/asset=BTC/interval=15m/date=2024-01-01.parquet"]
    assert list(windows["interval"]) == ["15m"]
    assert len(ticks) == 900


def test_asset_order_client_accepts_canonical(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        _add_window(session, asset="BTC")
        _add_window(session, asset="SOL", start_ts=WINDOW_START + 300)

    spec = parse_spec(
        start=PERIOD_START,
        end=PERIOD_END,
        assets=["SOL", "BTC"],
        intervals=["5m"],
    )
    dest = datasets / "order" / "dataset.zip"
    build_dataset_zip(spec, dest, dataset_id="ds_ord", data_revision="o1")
    load_dataset_zip(
        dest,
        start="2024-01-01T00:00:00Z",
        end="2024-01-02T00:00:00Z",
        assets=["SOL", "BTC"],
        intervals=["5m"],
    )


def test_corrupt_archive_incomplete_and_logged(tmp_path, monkeypatch, caplog):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        window = _add_window(session, archived=True, n_ticks=0)
        path = archive_path_for(window.asset, window.interval, window.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a parquet file")
        window_id = window.id

    caplog.set_level("WARNING")
    dest = datasets / "bad-arch" / "dataset.zip"
    built = build_dataset_zip(_spec(), dest, dataset_id="ds_bad", data_revision="b1")
    assert built.window_count == 1
    assert window_id in built.manifest["incomplete_window_ids"]
    assert "corrupt archive" in caplog.text


def test_sha256_mismatch_rejected(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        _add_window(session)
    dest = datasets / "hash" / "dataset.zip"
    build_dataset_zip(_spec(), dest, dataset_id="ds_hash", data_revision="h1")
    import zipfile

    tampered = datasets / "hash" / "tampered.zip"
    with zipfile.ZipFile(dest) as src, zipfile.ZipFile(tampered, "w") as out:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "windows.parquet":
                data = data + b"x"
            out.writestr(info, data)
    try:
        load_dataset_zip(tampered)
        assert False, "expected DatasetClientError"
    except DatasetClientError as exc:
        assert "sha256" in str(exc).lower()


def test_tick_repair_and_outcome_change_new_ids(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        window = _add_window(session, outcome=None, n_ticks=300)
        window_id = window.id

    service = DatasetService(store=DatasetStore(directory=datasets))
    service.start()
    first, _ = service.submit(_spec())
    ready = _wait_ready(service, first.dataset_id)
    id_unresolved = ready.dataset_id

    with get_session() as session:
        row = session.query(Window).filter_by(id=window_id).one()
        row.outcome = "UP"
        row.resolved_at = datetime(2024, 1, 1, 12, 20, tzinfo=timezone.utc)

    second, created = service.submit(_spec())
    assert created is True
    ready2 = _wait_ready(service, second.dataset_id)
    assert ready2.dataset_id != id_unresolved

    with get_session() as session:
        session.query(Tick).filter_by(window_id=window_id).delete()

    third, created_ticks = service.submit(_spec())
    assert created_ticks is True
    ready3 = _wait_ready(service, third.dataset_id)
    assert ready3.dataset_id != ready2.dataset_id
    service._pool.shutdown(wait=False, cancel_futures=True)


def test_failed_build_sanitized_then_retry(tmp_path, monkeypatch):
    datasets = _isolate(tmp_path, monkeypatch)
    with get_session() as session:
        _add_window(session)

    real = build_dataset_zip
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full at C:\\secret\\pmanalysis.db")
        return real(*args, **kwargs)

    monkeypatch.setattr("pmanalysis.datasets.service.build_dataset_zip", flaky)
    service = DatasetService(store=DatasetStore(directory=datasets))
    service.start()
    first, _ = service.submit(_spec())
    deadline = time.time() + 15
    failed = None
    while time.time() < deadline:
        failed = service.store.get(first.dataset_id)
        if failed and failed.status == "failed":
            break
        time.sleep(0.05)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "build failed"
    assert "secret" not in (failed.error or "")

    second, created = service.submit(_spec())
    assert created is True
    ready = _wait_ready(service, second.dataset_id)
    assert ready.status == "ready"
    service._pool.shutdown(wait=False, cancel_futures=True)


def test_download_409_while_building(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    app = create_app()
    with TestClient(app) as client:
        service = client.app.state.dataset_service
        record = DatasetRecord(
            dataset_id="ds_stuck",
            spec=_spec(),
            data_revision="x",
            status="building",
            windows_done=2,
            windows_total=10,
            tick_rows=50,
        )
        service.store.put(record)
        download = client.get("/v1/datasets/ds_stuck/download")
        assert download.status_code == 409
        status = client.get("/v1/datasets/ds_stuck")
        body = status.json()
        assert body["status"] == "building"
        assert body["windows_done"] == 2
        assert body["windows_total"] == 10
        assert body["tick_rows"] == 50


def test_start_logs_public_export(tmp_path, monkeypatch, caplog):
    datasets = _isolate(tmp_path, monkeypatch)
    caplog.set_level("INFO")
    DatasetService(store=DatasetStore(directory=datasets)).start()
    assert "dataset export is public on /v1" in caplog.text


def test_fetch_dataset_retries_after_404(tmp_path, monkeypatch):
    import httpx
    from pmanalysis.datasets import client as client_mod

    class Resp:
        def __init__(self, status, json_body=None, text="", content=b""):
            self.status_code = status
            self._json = json_body or {}
            self.text = text
            self.content = content

        def json(self):
            return self._json

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.posts = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            self.posts += 1
            return Resp(202, {"dataset_id": f"ds_{self.posts}", "status": "building"})

        def get(self, url, headers=None):
            if self.posts == 1:
                return Resp(404, text="gone")
            if url.endswith("/download"):
                return Resp(200, content=b"zip-bytes")
            return Resp(200, {"status": "ready", "dataset_id": "ds_2", "data_revision": "aa"})

    slept: list[float] = []
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(client_mod, "load_dataset_zip", lambda *a, **k: ({}, None, None))
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    dest = tmp_path / "out.zip"
    path = fetch_dataset(
        base_url="http://collector:8420",
        start="2024-01-01T00:00:00Z",
        end="2024-01-02T00:00:00Z",
        assets=["BTC"],
        intervals=["5m"],
        dest=dest,
        timeout_sec=5,
        poll_sec=0.25,
    )
    assert path == dest
    assert dest.read_bytes() == b"zip-bytes"
    assert slept
    assert slept[0] == 0.25


def test_public_download_window_is_last_30_days(monkeypatch):
    monkeypatch.setattr(config, "DATASET_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(config, "DATASET_MAX_RANGE_DAYS", 31)
    monkeypatch.setattr(config, "OWNER_CONTACT", "https://example.test/contact")
    now = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)
    now_ts = int(now.timestamp())
    ok = parse_spec(
        start=datetime(2026, 10, 1, tzinfo=timezone.utc),  # midnight 30 days back
        end=now,
        assets=["BTC"],
        intervals=["5m"],
        now_ts=now_ts,
    )
    assert ok.start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(SpecError, match="last 30 days.*example.test/contact"):
        parse_spec(
            start=datetime(2026, 9, 30, 23, tzinfo=timezone.utc),
            end=datetime(2026, 10, 2, tzinfo=timezone.utc),
            assets=["BTC"],
            intervals=["5m"],
            now_ts=now_ts,
        )
    with pytest.raises(SpecError, match="cannot exceed 31 days"):
        parse_spec(
            start=datetime(2026, 10, 1, tzinfo=timezone.utc),
            end=datetime(2026, 11, 2, 1, tzinfo=timezone.utc),
            assets=["BTC"],
            intervals=["5m"],
            now_ts=now_ts,
        )


def test_api_rejects_old_range_with_contact(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "DATASET_LOOKBACK_DAYS", 30)
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/datasets",
            json={
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-02T00:00:00Z",
                "assets": ["BTC"],
                "intervals": ["5m"],
            },
        )
    assert response.status_code == 400
    assert "last 30 days" in response.json()["detail"]
    assert config.OWNER_CONTACT in response.json()["detail"]


def test_csv_export_follows_public_window(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    with get_session() as session:
        old_id = _add_window(session, n_ticks=5).id  # dated 2024
        session.commit()
    monkeypatch.setattr(config, "DATASET_LOOKBACK_DAYS", 30)
    with TestClient(create_app()) as client:
        response = client.get(f"/api/export/{old_id}?format=csv")
    assert response.status_code == 403
    assert "last 30 days" in response.json()["detail"]

    monkeypatch.setattr(config, "DATASET_LOOKBACK_DAYS", 100_000)
    with TestClient(create_app()) as client:
        response = client.get(f"/api/export/{old_id}?format=csv")
    assert response.status_code == 200
    header = response.text.splitlines()[0].split(",")
    assert {"yes_mid", "secs_remaining", "combined_ask"} <= set(header)  # derived on export
