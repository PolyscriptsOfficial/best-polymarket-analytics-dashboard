"""SQLite catalog queries for coverage, fingerprints, and closed windows."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from pmanalysis import config
from pmanalysis.datasets.ids import data_revision, request_key
from pmanalysis.datasets.schema import DatasetSpec, WindowRow
from pmanalysis.db.models import Tick, Window
from pmanalysis.export.parquet import archive_path_for

# (catalog_sig, stats, monotonic_ts) keyed by request_key. Avoids restatting
# thousands of archive files on every cache-hit POST.
_ARCHIVE_FP_TTL_SEC = 60.0
_archive_fp_lock = threading.Lock()
_archive_fp_cache: dict[str, tuple[str, tuple[int, int, int], float]] = {}


def _closed_filters(now_ts: int):
    return (
        Window.start_ts > 0,
        Window.end_ts <= now_ts,
    )


def _spec_filters(spec: DatasetSpec):
    return (
        Window.asset.in_(spec.assets),
        Window.interval.in_(spec.intervals),
        Window.start_ts >= spec.start_ts,
        Window.start_ts < spec.end_ts,
        *_closed_filters(spec.now_ts),
    )


def _unresolved_expr():
    return case((or_(Window.outcome.is_(None), Window.outcome == ""), 1), else_=0)


def _archived_expr():
    return case((Window.archived.is_(True), 1), else_=0)


def coverage_rows(
    session: Session,
    *,
    asset: str | None = None,
    interval: str | None = None,
    now_ts: int | None = None,
) -> list[dict]:
    """Per-market history span. `archived_window_count` is parquet-backed windows."""
    now = int(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp())
    query = (
        session.query(
            Window.asset,
            Window.interval,
            func.min(Window.start_ts),
            func.max(Window.start_ts),
            func.count(Window.id),
            func.sum(_archived_expr()),
            func.sum(_unresolved_expr()),
        )
        .filter(*_closed_filters(now))
        .group_by(Window.asset, Window.interval)
    )
    if asset:
        query = query.filter(Window.asset == asset.upper())
    if interval:
        query = query.filter(Window.interval == interval)

    found: dict[tuple[str, str], dict] = {}
    for row in query.all():
        found[(str(row[0]).upper(), str(row[1]))] = {
            "asset": str(row[0]).upper(),
            "interval": str(row[1]),
            "first_start_ts": int(row[2]) if row[2] is not None else None,
            "last_closed_start_ts": int(row[3]) if row[3] is not None else None,
            "window_count": int(row[4] or 0),
            "archived_window_count": int(row[5] or 0),
            "unresolved_window_count": int(row[6] or 0),
        }

    assets = (asset.upper(),) if asset else config.ASSETS
    intervals = (interval,) if interval else config.INTERVALS
    empty = {
        "first_start_ts": None,
        "last_closed_start_ts": None,
        "window_count": 0,
        "archived_window_count": 0,
        "unresolved_window_count": 0,
    }
    items: list[dict] = []
    for market_asset in assets:
        for market_interval in intervals:
            row = found.get((market_asset, market_interval))
            if row is None:
                items.append({"asset": market_asset, "interval": market_interval, **empty})
            else:
                items.append(row)
    return items


def fingerprint_revision(
    session: Session, spec: DatasetSpec, *, refresh_archive: bool = False
) -> str:
    row = (
        session.query(
            func.count(Window.id),
            func.max(Window.id),
            func.max(Window.resolved_at),
            func.sum(
                case(
                    (and_(Window.outcome.isnot(None), Window.outcome != ""), 1),
                    else_=0,
                )
            ),
            func.sum(_archived_expr()),
        )
        .filter(*_spec_filters(spec))
        .one()
    )
    resolved_at = row[2]
    if isinstance(resolved_at, datetime):
        resolved_s = resolved_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        resolved_s = str(resolved_at or "")
    tick_rows, max_tick_ts_ms = _tick_fingerprint(session, spec)
    catalog_sig = (
        f"{int(row[0] or 0)}:{int(row[1] or 0)}:{resolved_s}:"
        f"{int(row[3] or 0)}:{int(row[4] or 0)}:{tick_rows}:{max_tick_ts_ms}"
    )
    if refresh_archive:
        archive_files, archive_bytes, archive_mtime_ns = _archive_fingerprint(session, spec)
        with _archive_fp_lock:
            _archive_fp_cache.pop(request_key(spec), None)
    else:
        archive_files, archive_bytes, archive_mtime_ns = _cached_archive_fingerprint(
            session, spec, catalog_sig
        )
    return data_revision(
        window_count=int(row[0] or 0),
        max_id=int(row[1]) if row[1] is not None else None,
        max_resolved_at=resolved_s,
        resolved_count=int(row[3] or 0),
        archived_count=int(row[4] or 0),
        tick_rows=tick_rows,
        max_tick_ts_ms=max_tick_ts_ms,
        archive_files=archive_files,
        archive_bytes=archive_bytes,
        archive_mtime_ns=archive_mtime_ns,
    )


def clear_archive_fingerprint_cache() -> None:
    with _archive_fp_lock:
        _archive_fp_cache.clear()


def _cached_archive_fingerprint(
    session: Session, spec: DatasetSpec, catalog_sig: str
) -> tuple[int, int, int]:
    key = request_key(spec)
    now = time.monotonic()
    with _archive_fp_lock:
        hit = _archive_fp_cache.get(key)
        if hit is not None and hit[0] == catalog_sig and (now - hit[2]) < _ARCHIVE_FP_TTL_SEC:
            return hit[1]
    stats = _archive_fingerprint(session, spec)
    with _archive_fp_lock:
        _archive_fp_cache[key] = (catalog_sig, stats, now)
    return stats


def _tick_fingerprint(session: Session, spec: DatasetSpec) -> tuple[int, int]:
    row = (
        session.query(func.count(Tick.id), func.max(Tick.ts_ms))
        .select_from(Tick)
        .join(Window, Window.id == Tick.window_id)
        .filter(*_spec_filters(spec))
        .one()
    )
    return int(row[0] or 0), int(row[1] or 0)


def _archive_fingerprint(session: Session, spec: DatasetSpec) -> tuple[int, int, int]:
    rows = (
        session.query(Window.asset, Window.interval, Window.slug)
        .filter(*_spec_filters(spec), Window.archived.is_(True))
        .all()
    )
    files = 0
    total_bytes = 0
    max_mtime_ns = 0
    for asset, interval, slug in rows:
        path = archive_path_for(str(asset), str(interval), str(slug))
        if not path.exists():
            continue
        stat = path.stat()
        files += 1
        total_bytes += int(stat.st_size)
        max_mtime_ns = max(max_mtime_ns, int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))))
    return files, total_bytes, max_mtime_ns


def list_closed_windows(session: Session, spec: DatasetSpec) -> list[WindowRow]:
    rows = (
        session.query(
            Window.id,
            Window.slug,
            Window.asset,
            Window.interval,
            Window.start_ts,
            Window.end_ts,
            Window.strike_price,
            Window.opening_oracle_price,
            Window.final_oracle_price,
            Window.outcome,
            Window.archived,
        )
        .filter(*_spec_filters(spec))
        .order_by(Window.start_ts.asc(), Window.id.asc())
        .all()
    )
    out: list[WindowRow] = []
    for row in rows:
        outcome = row[9]
        if outcome == "":
            outcome = None
        out.append(
            WindowRow(
                window_id=int(row[0]),
                slug=str(row[1]),
                asset=str(row[2]).upper(),
                interval=str(row[3]),
                start_ts=int(row[4]),
                end_ts=int(row[5]),
                strike_price=_float(row[6]),
                opening_oracle_price=_float(row[7]),
                final_oracle_price=_float(row[8]),
                outcome=str(outcome) if outcome is not None else None,
                archived=bool(row[10]),
            )
        )
    return out


def _float(value) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number != number:
        return None
    return number
