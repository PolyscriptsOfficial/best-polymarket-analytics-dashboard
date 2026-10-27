"""On-disk dataset cache: one folder per id, zip + meta.json."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pmanalysis import config
from pmanalysis.datasets.ids import zip_filename
from pmanalysis.datasets.schema import DatasetSpec

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class DatasetRecord:
    dataset_id: str
    spec: DatasetSpec
    data_revision: str
    status: str
    error: str | None = None
    window_count: int = 0
    windows_done: int = 0
    windows_total: int = 0
    tick_rows: int = 0
    size_bytes: int = 0
    filename: str = ""
    created_at: datetime = field(default_factory=_now)
    last_access_at: datetime = field(default_factory=_now)
    zip_path: Path | None = None

    def touch(self) -> None:
        self.last_access_at = _now()

    def public(self) -> dict:
        payload = {
            "dataset_id": self.dataset_id,
            "status": self.status,
            "schema_version": 1,
            "data_revision": self.data_revision,
            "start": self.spec.iso_start(),
            "end": self.spec.iso_end(),
            "assets": list(self.spec.assets),
            "intervals": list(self.spec.intervals),
        }
        if self.status == "building":
            payload.update(
                {
                    "windows_done": self.windows_done,
                    "windows_total": self.windows_total,
                    "tick_rows": self.tick_rows,
                }
            )
        if self.status == "ready":
            payload.update(
                {
                    "window_count": self.window_count,
                    "tick_rows": self.tick_rows,
                    "bytes": self.size_bytes,
                    "download_path": f"/v1/datasets/{self.dataset_id}/download",
                }
            )
        if self.status == "failed" and self.error:
            payload["error"] = self.error
        return payload

    def to_meta(self) -> dict:
        return {
            "dataset_id": self.dataset_id,
            "status": self.status,
            "data_revision": self.data_revision,
            "start": self.spec.iso_start(),
            "end": self.spec.iso_end(),
            "assets": list(self.spec.assets),
            "intervals": list(self.spec.intervals),
            "now_ts": self.spec.now_ts,
            "error": self.error,
            "window_count": self.window_count,
            "windows_done": self.windows_done,
            "windows_total": self.windows_total,
            "tick_rows": self.tick_rows,
            "bytes": self.size_bytes,
            "filename": self.filename,
            "created_at": _iso(self.created_at),
            "last_access_at": _iso(self.last_access_at),
        }


class DatasetStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or config.DATASET_DIR
        self._lock = threading.RLock()
        self._records: dict[str, DatasetRecord] = {}

    def restore(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for child in self.directory.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            zip_path = child / "dataset.zip"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                record = self._from_meta(meta, zip_path)
            except Exception:
                log.warning("skipping corrupt dataset meta %s", meta_path)
                continue
            if record.status == "building":
                shutil.rmtree(child, ignore_errors=True)
                continue
            if record.status == "ready" and (record.zip_path is None or not record.zip_path.exists()):
                shutil.rmtree(child, ignore_errors=True)
                continue
            self._records[record.dataset_id] = record

    def get(self, dataset_id: str) -> DatasetRecord | None:
        with self._lock:
            return self._records.get(dataset_id)

    def put(self, record: DatasetRecord) -> None:
        with self._lock:
            self._records[record.dataset_id] = record
            self._write_meta(record)

    def touch(self, record: DatasetRecord) -> None:
        with self._lock:
            record.touch()
            self._write_meta(record)

    def workdir(self, dataset_id: str) -> Path:
        path = self.directory / dataset_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def zip_path(self, dataset_id: str) -> Path:
        return self.workdir(dataset_id) / "dataset.zip"

    def sweep_expired(self) -> int:
        cutoff = _now().timestamp() - config.DATASET_TTL_DAYS * 86400
        removed = 0
        with self._lock:
            stale_ids = [
                dataset_id
                for dataset_id, record in self._records.items()
                if record.status != "building"
                and record.last_access_at.timestamp() < cutoff
            ]
            for dataset_id in stale_ids:
                self._records.pop(dataset_id, None)
                shutil.rmtree(self.directory / dataset_id, ignore_errors=True)
                removed += 1
        if removed:
            log.info("removed %s expired dataset(s)", removed)
        return removed

    def _write_meta(self, record: DatasetRecord) -> None:
        path = self.workdir(record.dataset_id) / "meta.json"
        path.write_text(json.dumps(record.to_meta(), indent=2) + "\n", encoding="utf-8")

    def _from_meta(self, meta: dict, zip_path: Path) -> DatasetRecord:
        spec = DatasetSpec(
            start=_parse_iso(meta["start"]),
            end=_parse_iso(meta["end"]),
            assets=tuple(meta["assets"]),
            intervals=tuple(meta["intervals"]),
            now_ts=int(meta.get("now_ts") or 0),
        )
        status = str(meta.get("status") or "failed")
        record = DatasetRecord(
            dataset_id=str(meta["dataset_id"]),
            spec=spec,
            data_revision=str(meta.get("data_revision") or ""),
            status=status,
            error=meta.get("error"),
            window_count=int(meta.get("window_count") or 0),
            windows_done=int(meta.get("windows_done") or 0),
            windows_total=int(meta.get("windows_total") or 0),
            tick_rows=int(meta.get("tick_rows") or 0),
            size_bytes=int(meta.get("bytes") or 0),
            filename=str(meta.get("filename") or zip_filename(spec)),
            created_at=_parse_iso(meta["created_at"]) if meta.get("created_at") else _now(),
            last_access_at=_parse_iso(meta["last_access_at"]) if meta.get("last_access_at") else _now(),
            zip_path=zip_path if zip_path.exists() else None,
        )
        return record
