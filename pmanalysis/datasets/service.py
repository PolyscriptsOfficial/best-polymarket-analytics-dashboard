"""Build and serve history datasets on a background thread pool."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from pmanalysis import config
from pmanalysis.datasets.builder import build_dataset_zip
from pmanalysis.datasets.ids import dataset_id, zip_filename
from pmanalysis.datasets.query import coverage_rows, fingerprint_revision
from pmanalysis.datasets.schema import DatasetSpec
from pmanalysis.datasets.store import DatasetRecord, DatasetStore
from pmanalysis.db.session import get_session

log = logging.getLogger(__name__)

_service: DatasetService | None = None


def set_dataset_service(service: DatasetService | None) -> None:
    global _service
    _service = service


def get_dataset_service() -> DatasetService | None:
    return _service


class DatasetService:
    def __init__(self, store: DatasetStore | None = None) -> None:
        self.store = store or DatasetStore()
        self._pool = ThreadPoolExecutor(
            max_workers=config.DATASET_BUILD_WORKERS,
            thread_name_prefix="dataset-build",
        )
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._sweep_task: asyncio.Task | None = None

    def start(self) -> None:
        config.ensure_data_dirs()
        self.store.restore()
        log.info("dataset export is public on /v1 (no bearer token)")

    async def start_background(self) -> None:
        self.start()
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="dataset-sweep")

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None
        self._pool.shutdown(wait=False, cancel_futures=True)

    def coverage(
        self,
        *,
        asset: str | None = None,
        interval: str | None = None,
    ) -> dict:
        with get_session() as session:
            items = coverage_rows(session, asset=asset, interval=interval)
        return {"schema_version": 1, "markets": items}

    def submit(self, spec: DatasetSpec, *, force: bool = False) -> tuple[DatasetRecord, bool]:
        """Return (record, accepted_new_build)."""
        with get_session() as session:
            revision = fingerprint_revision(session, spec, refresh_archive=force)
        ident = dataset_id(spec, revision)
        with self._lock:
            existing = self.store.get(ident)
            if (
                existing
                and existing.status == "ready"
                and existing.zip_path
                and existing.zip_path.exists()
                and not force
            ):
                return existing, False
            if existing and existing.status == "building":
                return existing, False

            record = DatasetRecord(
                dataset_id=ident,
                spec=spec,
                data_revision=revision,
                status="building",
                filename=zip_filename(spec),
            )
            self.store.put(record)
            self._inflight.add(ident)
            self._pool.submit(self._run_build, ident)
            return record, True

    def _run_build(self, ident: str) -> None:
        record = self.store.get(ident)
        if record is None:
            return
        try:
            def _progress(done: int, total: int, tick_rows: int) -> None:
                record.windows_done = done
                record.windows_total = total
                record.tick_rows = tick_rows

            built = build_dataset_zip(
                record.spec,
                self.store.zip_path(ident),
                dataset_id=ident,
                data_revision=record.data_revision,
                on_progress=_progress,
            )
            record.status = "ready"
            record.error = None
            record.window_count = built.window_count
            record.windows_done = built.window_count
            record.windows_total = built.window_count
            record.tick_rows = built.tick_rows
            record.size_bytes = built.size_bytes
            record.filename = built.filename
            record.zip_path = built.zip_path
            record.touch()
            self.store.put(record)
        except Exception:
            log.exception("dataset build failed id=%s", ident)
            record.status = "failed"
            record.error = "build failed"
            self.store.put(record)
        finally:
            self._inflight.discard(ident)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(self.store.sweep_expired)
            except Exception:
                log.exception("dataset sweep failed")
