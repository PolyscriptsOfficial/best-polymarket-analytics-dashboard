"""Stable dataset ids, cache keys, and download filenames."""

from __future__ import annotations

import hashlib

from pmanalysis import config
from pmanalysis.datasets.schema import CONTRACT_REVISION, SCHEMA_VERSION, DatasetSpec


def request_key(spec: DatasetSpec) -> str:
    return "|".join(
        (
            f"v{SCHEMA_VERSION}r{CONTRACT_REVISION}",
            spec.iso_start(),
            spec.iso_end(),
            ",".join(spec.assets),
            ",".join(spec.intervals),
        )
    )


def data_revision(
    *,
    window_count: int,
    max_id: int | None,
    max_resolved_at: str,
    resolved_count: int,
    archived_count: int,
    tick_rows: int = 0,
    max_tick_ts_ms: int = 0,
    archive_files: int = 0,
    archive_bytes: int = 0,
    archive_mtime_ns: int = 0,
) -> str:
    payload = (
        f"{window_count}:{max_id or 0}:{max_resolved_at}:"
        f"{resolved_count}:{archived_count}:"
        f"{tick_rows}:{max_tick_ts_ms}:"
        f"{archive_files}:{archive_bytes}:{archive_mtime_ns}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def dataset_id(spec: DatasetSpec, revision: str) -> str:
    payload = f"{request_key(spec)}|{revision}"
    return "ds_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def zip_filename(spec: DatasetSpec) -> str:
    if spec.assets == config.ASSETS:
        market = "all"
    else:
        market = "-".join(asset.lower() for asset in spec.assets)
    intervals = "-".join(spec.intervals)
    start = spec.start.strftime("%Y%m%d")
    end = spec.end.strftime("%Y%m%d")
    return f"{market}_{intervals}_{start}_{end}.zip"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
