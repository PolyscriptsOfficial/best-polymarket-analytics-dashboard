"""Build a period dataset zip from the window catalog + 1s ticks."""

from __future__ import annotations

import json
import logging
import zipfile
from collections.abc import Callable
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pmanalysis.datasets.completeness import evaluate_completeness
from pmanalysis.datasets.ids import sha256_bytes, zip_filename
from pmanalysis.datasets.query import list_closed_windows
from pmanalysis.datasets.schema import (
    PRICE_UNIT,
    SCHEMA_VERSION,
    TICK_SCHEMA,
    TIME_UNIT,
    WINDOW_SCHEMA,
    DatasetSpec,
    WindowRow,
    empty_table,
    utc_date,
)
from pmanalysis.datasets.ticks import load_window_tables
from pmanalysis.db.session import get_session

log = logging.getLogger(__name__)

_PARQUET_OPTS = {
    "compression": "zstd",
    "compression_level": 3,
    "use_dictionary": True,
}


@dataclass(frozen=True, slots=True)
class BuiltDataset:
    zip_path: Path
    filename: str
    window_count: int
    tick_rows: int
    size_bytes: int
    data_revision: str
    manifest: dict


ProgressCb = Callable[[int, int, int], None]


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, **_PARQUET_OPTS)
    return sink.getvalue().to_pybytes()


def _window_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return empty_table(WINDOW_SCHEMA)
    payload = {
        "window_id": [row["window_id"] for row in rows],
        "slug": [row["slug"] for row in rows],
        "asset": [row["asset"] for row in rows],
        "interval": [row["interval"] for row in rows],
        "start_ts": [row["start_ts"] for row in rows],
        "end_ts": [row["end_ts"] for row in rows],
        "strike_price": [row["strike_price"] for row in rows],
        "opening_oracle_price": [row["opening_oracle_price"] for row in rows],
        "final_oracle_price": [row["final_oracle_price"] for row in rows],
        "twap_initial": [row["twap_initial"] for row in rows],
        "final_twap": [row["final_twap"] for row in rows],
        "outcome": [row["outcome"] for row in rows],
        "outcome_source": [row["outcome_source"] for row in rows],
        "tick_count": [row["tick_count"] for row in rows],
        "complete": [row["complete"] for row in rows],
    }
    return pa.Table.from_pydict(payload, schema=WINDOW_SCHEMA)


def _tick_path(window: WindowRow) -> str:
    return (
        f"ticks/asset={window.asset}/interval={window.interval}/"
        f"date={utc_date(window.start_ts)}.parquet"
    )


def _concat(tables: list[pa.Table]) -> pa.Table:
    valid = [table for table in tables if table.num_rows]
    if not valid:
        return empty_table(TICK_SCHEMA)
    if len(valid) == 1:
        return valid[0]
    return pa.concat_tables(valid, promote_options="none")


def build_dataset_zip(
    spec: DatasetSpec,
    dest: Path,
    *,
    dataset_id: str,
    data_revision: str,
    on_progress: ProgressCb | None = None,
) -> BuiltDataset:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".zip.tmp")
    if tmp.exists():
        tmp.unlink()

    window_rows: list[dict] = []
    incomplete_ids: list[int] = []
    unresolved_ids: list[int] = []
    files: list[dict] = []
    tick_rows = 0
    filename = zip_filename(spec)

    with get_session() as session:
        windows = list_closed_windows(session, spec)

    grouped: dict[str, list[WindowRow]] = defaultdict(list)
    for window in windows:
        grouped[_tick_path(window)].append(window)
    total = len(windows)
    done = 0
    if on_progress is not None:
        on_progress(0, total, 0)

    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path in sorted(grouped):
            day_windows = grouped[path]
            with get_session() as session:
                tables = load_window_tables(session, day_windows)
            day_parts: list[pa.Table] = []
            for window in day_windows:
                table = tables.get(window.window_id)
                if table is None or table.num_rows == 0:
                    table = empty_table(TICK_SCHEMA)
                ts_ms = (
                    table.column("ts_ms").to_numpy(zero_copy_only=False)
                    if table.num_rows
                    else []
                )
                verdict = evaluate_completeness(
                    ts_ms,
                    start_ts=window.start_ts,
                    end_ts=window.end_ts,
                    interval=window.interval,
                )
                if table.num_rows:
                    day_parts.append(table)
                    tick_rows += table.num_rows
                if not verdict.complete:
                    incomplete_ids.append(window.window_id)
                if not window.outcome:
                    unresolved_ids.append(window.window_id)
                window_rows.append(
                    {
                        "window_id": window.window_id,
                        "slug": window.slug,
                        "asset": window.asset,
                        "interval": window.interval,
                        "start_ts": window.start_ts,
                        "end_ts": window.end_ts,
                        "strike_price": window.strike_price,
                        "opening_oracle_price": window.opening_oracle_price,
                        "final_oracle_price": window.final_oracle_price,
                        "twap_initial": window.twap_initial,
                        "final_twap": window.final_twap,
                        "outcome": window.outcome,
                        "outcome_source": window.outcome_source,
                        "tick_count": verdict.tick_count,
                        "complete": verdict.complete,
                    }
                )
                done += 1
                if on_progress is not None:
                    on_progress(done, total, tick_rows)
            if day_parts:
                day_table = _concat(day_parts)
                blob = _parquet_bytes(day_table)
                zf.writestr(path, blob)
                files.append(
                    {
                        "path": path,
                        "rows": day_table.num_rows,
                        "sha256": sha256_bytes(blob),
                    }
                )

        window_rows.sort(key=lambda row: (row["start_ts"], row["window_id"]))
        windows_blob = _parquet_bytes(_window_table(window_rows))
        zf.writestr("windows.parquet", windows_blob)
        files.insert(
            0,
            {
                "path": "windows.parquet",
                "rows": len(window_rows),
                "sha256": sha256_bytes(windows_blob),
            },
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "data_revision": data_revision,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start": spec.iso_start(),
            "end": spec.iso_end(),
            "assets": list(spec.assets),
            "intervals": list(spec.intervals),
            "window_count": len(window_rows),
            "complete_window_count": len(window_rows) - len(incomplete_ids),
            "unresolved_window_count": len(unresolved_ids),
            "tick_rows": tick_rows,
            "price_unit": PRICE_UNIT,
            "time_unit": TIME_UNIT,
            "files": files,
            "incomplete_window_ids": incomplete_ids,
            "unresolved_window_ids": unresolved_ids,
        }
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, separators=(",", ":"), ensure_ascii=True) + "\n",
        )

    tmp.replace(dest)
    size = dest.stat().st_size
    log.info(
        "dataset %s windows=%s ticks=%s bytes=%s file=%s",
        dataset_id,
        len(window_rows),
        tick_rows,
        size,
        filename,
    )
    return BuiltDataset(
        zip_path=dest,
        filename=filename,
        window_count=len(window_rows),
        tick_rows=tick_rows,
        size_bytes=size,
        data_revision=data_revision,
        manifest=manifest,
    )
