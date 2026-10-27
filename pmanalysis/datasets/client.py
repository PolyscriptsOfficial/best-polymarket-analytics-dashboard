"""HTTP client the backtest app can copy or import."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pandas as pd

from pmanalysis.datasets.schema import SCHEMA_VERSION, SpecError, canonical_assets, canonical_intervals

DEFAULT_TIMEOUT_SEC = 3600.0
DEFAULT_POLL_SEC = 1.0


class DatasetClientError(RuntimeError):
    pass


def _wait(deadline: float, poll_sec: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DatasetClientError("timed out waiting for dataset")
    time.sleep(min(poll_sec, remaining))


def _canonical_list(kind: str, values: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if values is None:
        return None
    try:
        if kind == "assets":
            return list(canonical_assets(values))
        return list(canonical_intervals(values))
    except SpecError as exc:
        raise DatasetClientError(str(exc)) from exc


def _verify_manifest_files(zf: zipfile.ZipFile, manifest: dict) -> None:
    for item in manifest.get("files") or []:
        path = item.get("path")
        expected = item.get("sha256")
        if not path or not expected:
            continue
        try:
            payload = zf.read(path)
        except KeyError as exc:
            raise DatasetClientError(f"missing zip member {path}") from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected:
            raise DatasetClientError(f"sha256 mismatch for {path}")


def load_dataset_zip(
    path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    assets: list[str] | None = None,
    intervals: list[str] | None = None,
    verify_hashes: bool = True,
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    want_assets = _canonical_list("assets", assets)
    want_intervals = _canonical_list("intervals", intervals)
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise DatasetClientError(
                f"unsupported schema_version {manifest.get('schema_version')}"
            )
        if start is not None and manifest.get("start") != start:
            raise DatasetClientError("period start mismatch")
        if end is not None and manifest.get("end") != end:
            raise DatasetClientError("period end mismatch")
        if want_assets is not None and list(manifest.get("assets") or []) != want_assets:
            raise DatasetClientError("assets mismatch")
        if want_intervals is not None and list(manifest.get("intervals") or []) != want_intervals:
            raise DatasetClientError("intervals mismatch")
        if verify_hashes:
            _verify_manifest_files(zf, manifest)
        windows = pd.read_parquet(io.BytesIO(zf.read("windows.parquet")))
        frames = [
            pd.read_parquet(io.BytesIO(zf.read(name)))
            for name in zf.namelist()
            if name.startswith("ticks/") and name.endswith(".parquet")
        ]
    ticks = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not windows.empty:
        usable = windows[windows["complete"] & windows["outcome"].notna()]
        if not ticks.empty:
            ticks = ticks[ticks["window_id"].isin(usable["window_id"])]
        windows = usable
    return manifest, windows, ticks


def fetch_dataset(
    *,
    base_url: str,
    start: str,
    end: str,
    assets: list[str],
    intervals: list[str],
    dest: Path,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    poll_sec: float = DEFAULT_POLL_SEC,
) -> Path:
    """POST /v1/datasets, poll until ready, write the zip to dest.

    Reuses `dest` when it already matches the returned dataset_id and data_revision.
    If the collector restarts and the id 404s, wait `poll_sec` then POST again
    until timeout.
    """
    import httpx

    assets = _canonical_list("assets", assets) or []
    intervals = _canonical_list("intervals", intervals) or []
    base = base_url.rstrip("/")
    body = {"start": start, "end": end, "assets": assets, "intervals": intervals}
    deadline = time.monotonic() + timeout_sec
    with httpx.Client(timeout=timeout_sec) as client:
        while True:
            if time.monotonic() >= deadline:
                raise DatasetClientError("timed out waiting for dataset")
            created = client.post(f"{base}/v1/datasets", json=body)
            if created.status_code >= 400:
                raise DatasetClientError(f"create failed: {created.status_code} {created.text}")
            payload = created.json()
            dataset_id = payload["dataset_id"]
            lost = False
            while True:
                if time.monotonic() >= deadline:
                    raise DatasetClientError("timed out waiting for dataset")
                status = client.get(f"{base}/v1/datasets/{dataset_id}")
                if status.status_code == 404:
                    lost = True
                    break
                if status.status_code >= 400:
                    raise DatasetClientError(
                        f"status failed: {status.status_code} {status.text}"
                    )
                payload = status.json()
                if payload["status"] == "ready":
                    if dest.exists() and _cached_zip_matches(
                        dest,
                        dataset_id=dataset_id,
                        data_revision=str(payload.get("data_revision") or ""),
                        start=start,
                        end=end,
                        assets=assets,
                        intervals=intervals,
                    ):
                        return dest
                    download = client.get(f"{base}/v1/datasets/{dataset_id}/download")
                    if download.status_code == 404:
                        lost = True
                        break
                    if download.status_code >= 400:
                        raise DatasetClientError(
                            f"download failed: {download.status_code} {download.text}"
                        )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(download.content)
                    load_dataset_zip(
                        dest, start=start, end=end, assets=assets, intervals=intervals
                    )
                    return dest
                if payload["status"] == "failed":
                    raise DatasetClientError(payload.get("error") or "dataset failed")
                _wait(deadline, poll_sec)
            if not lost:
                raise DatasetClientError("timed out waiting for dataset")
            _wait(deadline, poll_sec)


def _cached_zip_matches(
    path: Path,
    *,
    dataset_id: str,
    data_revision: str,
    start: str,
    end: str,
    assets: list[str],
    intervals: list[str],
) -> bool:
    try:
        manifest, _, _ = load_dataset_zip(
            path, start=start, end=end, assets=assets, intervals=intervals
        )
    except (DatasetClientError, OSError, zipfile.BadZipFile, ValueError):
        return False
    return (
        manifest.get("dataset_id") == dataset_id
        and manifest.get("data_revision") == data_revision
    )
