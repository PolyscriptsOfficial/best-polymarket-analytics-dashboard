"""Remove corrupt ticks; keep SQLite bounded after parquet archive."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, text

from pmanalysis.config import FEED_RECORD_ENABLED, TICK_RETENTION_HOURS
from pmanalysis.db.models import (
    BinanceTrade,
    Candle,
    ClobQuote,
    OracleTick,
    Tick,
    Window,
    WindowSnapshot,
)
from pmanalysis.db.session import get_engine, get_session, reset_engine
from pmanalysis.export.parquet import archive_path_for

log = logging.getLogger(__name__)

_RAW_FEED_MODELS = (BinanceTrade, OracleTick, ClobQuote)


def remove_impossible_quote_ticks(*, ask_threshold: float = 90.0) -> dict:
    """Delete ticks where both YES and NO ask are >= threshold (impossible quotes)."""
    with get_session() as session:
        bad_rows = (
            session.query(Tick.id, Tick.window_id, Tick.ts_ms)
            .filter(Tick.yes_ask >= ask_threshold, Tick.no_ask >= ask_threshold)
            .all()
        )
        if not bad_rows:
            return {"ticks_removed": 0, "windows_affected": 0}

        tick_ids = [row[0] for row in bad_rows]
        windows_affected = len({row[1] for row in bad_rows})

        ticks_removed = (
            session.query(Tick)
            .filter(Tick.id.in_(tick_ids))
            .delete(synchronize_session=False)
        )
        session.commit()

    return {"ticks_removed": ticks_removed, "windows_affected": windows_affected}


def purge_archived_ticks(
    *,
    retention_hours: int | None = None,
    batch_size: int = 250,
) -> dict:
    """Delete tick rows for archived windows past retention.

    Only windows that still have rows in ``ticks`` are considered, so sweeps stay
    fast after history grows (no rescanning tens of thousands of empty catalog ids).
    """
    hours = TICK_RETENTION_HOURS if retention_hours is None else retention_hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_session() as session:
        window_ids = [
            int(row[0])
            for row in session.query(Tick.window_id)
            .join(Window, Window.id == Tick.window_id)
            .filter(Window.archived.is_(True), Window.resolved_at < cutoff)
            .distinct()
            .order_by(Tick.window_id.asc())
            .all()
        ]

    if not window_ids:
        return {
            "ticks_removed": 0,
            "windows_affected": 0,
            "retention_hours": hours,
            "candidates": 0,
        }

    ticks_removed = 0
    windows_affected = 0
    for offset in range(0, len(window_ids), batch_size):
        batch = window_ids[offset : offset + batch_size]
        with get_session() as session:
            n_tick = (
                session.query(Tick)
                .filter(Tick.window_id.in_(batch))
                .delete(synchronize_session=False)
            )
            session.commit()
        ticks_removed += n_tick
        if n_tick:
            windows_affected += len(batch)

    if ticks_removed:
        log.info(
            "Retention purge: %d ticks removed across %d windows (archived, >%dh old)",
            ticks_removed,
            windows_affected,
            hours,
        )

    return {
        "ticks_removed": ticks_removed,
        "windows_affected": windows_affected,
        "retention_hours": hours,
        "candidates": len(window_ids),
    }


def purge_raw_feeds(*, retention_hours: int | None = None, rebuild: bool = False) -> dict:
    """TTL raw WebSocket tables, or rebuild them empty when explicitly asked.

    Live retention must not DROP multi-GB tables (that locks the UI for minutes).
    Use `python -m pmanalysis prune-db` while the collector is stopped to rebuild.
    """
    hours = TICK_RETENTION_HOURS if retention_hours is None else retention_hours
    result: dict = {"feed_record": FEED_RECORD_ENABLED, "retention_hours": hours}

    if not FEED_RECORD_ENABLED:
        engine = get_engine()
        for model in _RAW_FEED_MODELS:
            name = model.__tablename__
            with get_session() as session:
                nonempty = session.query(model.id).limit(1).first()
            if not nonempty:
                result[name] = 0
                continue
            if not rebuild:
                result[name] = "skipped"
                log.info("Raw feed table %s still has rows; skip drop while live (run prune-db)", name)
                continue
            approx = 0
            with get_session() as session:
                approx = int(session.query(func.max(model.id)).scalar() or 0)
            model.__table__.drop(engine, checkfirst=True)
            model.__table__.create(engine)
            result[name] = approx
            log.info("Dropped raw feed table %s (~%s id high-water)", name, approx)
        return result

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    with get_session() as session:
        for model in _RAW_FEED_MODELS:
            deleted = (
                session.query(model)
                .filter(model.ts_ms < cutoff_ms)
                .delete(synchronize_session=False)
            )
            result[model.__tablename__] = int(deleted or 0)
        session.commit()
    return result


def checkpoint_wal() -> None:
    """Flush the WAL back into the main DB file so it doesn't grow unbounded."""
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


def vacuum_db() -> None:
    """Return free pages to the OS. Exclusive; stop the collector first on large files."""
    engine = get_engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))


def _window_row_from_db(row) -> "WindowRow":
    from pmanalysis.datasets.schema import WindowRow

    outcome = row.outcome
    if outcome == "":
        outcome = None
    return WindowRow(
        window_id=int(row.id),
        slug=str(row.slug),
        asset=str(row.asset).upper(),
        interval=str(row.interval),
        start_ts=int(row.start_ts),
        end_ts=int(row.end_ts),
        strike_price=float(row.strike_price) if row.strike_price is not None else None,
        opening_oracle_price=(
            float(row.opening_oracle_price) if row.opening_oracle_price is not None else None
        ),
        final_oracle_price=(
            float(row.final_oracle_price) if row.final_oracle_price is not None else None
        ),
        outcome=str(outcome) if outcome is not None else None,
        archived=bool(row.archived),
    )


def _broken_window_reason(window: "WindowRow", ts_ms, *, strict: bool) -> str | None:
    from pmanalysis.datasets.completeness import evaluate_completeness

    if not window.outcome:
        return "unresolved"
    if ts_ms is None or len(ts_ms) == 0:
        return "no_ticks"
    if not strict:
        return None
    comp = evaluate_completeness(
        ts_ms,
        start_ts=window.start_ts,
        end_ts=window.end_ts,
        interval=window.interval,
    )
    if not comp.complete:
        return "incomplete"
    return None


def purge_broken_windows(*, dry_run: bool = False, batch_size: int = 250, strict: bool = False) -> dict:
    """Delete closed windows that are not usable for backtests.

    Default (``strict=False``): missing outcome, or no tick rows in SQLite/parquet.
    ``strict=True`` also drops windows that fail dataset ``complete`` rules
    (95% fill, 3s edges, 5s max gap) — can remove most history if feeds were gappy.

    Removes SQLite ticks, the window catalog row, its candle, and parquet archive.
    Stop the collector first so it does not recreate deleted slugs mid-pass.
    """
    from pmanalysis.datasets.schema import SQLITE_TICK_BATCH
    from pmanalysis.datasets.ticks import load_window_tables

    now_ts = int(datetime.now(timezone.utc).timestamp())
    reasons: dict[str, int] = {}
    to_remove: list[tuple[int, str, str, str, int]] = []

    with get_session() as session:
        closed = (
            session.query(Window)
            .filter(Window.end_ts < now_ts)
            .order_by(Window.id.asc())
            .all()
        )
        for offset in range(0, len(closed), batch_size):
            chunk = closed[offset : offset + batch_size]
            rows = [_window_row_from_db(window) for window in chunk]
            tables = load_window_tables(session, rows)
            for window in rows:
                if window.archived:
                    path = archive_path_for(window.asset, window.interval, window.slug)
                    if not path.exists():
                        reasons["missing_archive"] = reasons.get("missing_archive", 0) + 1
                        to_remove.append(
                            (
                                window.window_id,
                                window.slug,
                                window.asset,
                                window.interval,
                                window.start_ts,
                            )
                        )
                        continue
                table = tables.get(window.window_id)
                ts_ms = (
                    table.column("ts_ms").to_numpy()
                    if table is not None and table.num_rows
                    else []
                )
                reason = _broken_window_reason(window, ts_ms, strict=strict)
                if reason is None:
                    continue
                reasons[reason] = reasons.get(reason, 0) + 1
                to_remove.append(
                    (window.window_id, window.slug, window.asset, window.interval, window.start_ts)
                )

    if dry_run or not to_remove:
        return {
            "dry_run": dry_run,
            "strict": strict,
            "windows_removed": 0,
            "parquet_removed": 0,
            "candles_removed": 0,
            "ticks_removed": 0,
            "reasons": reasons,
            "candidates": len(to_remove),
        }

    ticks_removed = 0
    candles_removed = 0
    parquet_removed = 0
    windows_removed = 0

    for offset in range(0, len(to_remove), SQLITE_TICK_BATCH):
        batch = to_remove[offset : offset + SQLITE_TICK_BATCH]
        batch_ids = [item[0] for item in batch]
        with get_session() as session:
            ticks_removed += (
                session.query(Tick)
                .filter(Tick.window_id.in_(batch_ids))
                .delete(synchronize_session=False)
            )
            session.query(WindowSnapshot).filter(
                WindowSnapshot.window_id.in_(batch_ids)
            ).delete(synchronize_session=False)
            for window_id, slug, asset, interval, start_ts in batch:
                candles_removed += (
                    session.query(Candle)
                    .filter_by(asset=asset, interval=interval, ts=start_ts)
                    .delete(synchronize_session=False)
                )
                path = archive_path_for(asset, interval, slug)
                if path.exists():
                    path.unlink()
                    parquet_removed += 1
            windows_removed += (
                session.query(Window)
                .filter(Window.id.in_(batch_ids))
                .delete(synchronize_session=False)
            )
            session.commit()

    log.info(
        "Purged broken windows: %d removed (%s)",
        windows_removed,
        ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()) if v),
    )
    checkpoint_wal()
    from pmanalysis.datasets.query import clear_archive_fingerprint_cache

    clear_archive_fingerprint_cache()

    return {
        "dry_run": False,
        "strict": strict,
        "windows_removed": windows_removed,
        "parquet_removed": parquet_removed,
        "candles_removed": candles_removed,
        "ticks_removed": ticks_removed,
        "reasons": reasons,
        "candidates": len(to_remove),
    }


def run_retention(*, rebuild_raw: bool = False) -> dict:
    """One retention pass: prune archived ticks, raw feeds, then checkpoint WAL."""
    ticks = purge_archived_ticks()
    feeds = purge_raw_feeds(rebuild=rebuild_raw)
    if ticks.get("ticks_removed"):
        checkpoint_wal()
    from pmanalysis.datasets.query import clear_archive_fingerprint_cache

    clear_archive_fingerprint_cache()
    return {**ticks, "raw_feeds": feeds}


def _keep_window_ids(conn: sqlite3.Connection, cutoff: datetime) -> list[int]:
    rows = conn.execute("SELECT id, archived, resolved_at FROM windows").fetchall()
    keep: list[int] = []
    naive_cutoff = cutoff.replace(tzinfo=None)
    for window_id, archived, resolved_at in rows:
        if not archived:
            keep.append(int(window_id))
            continue
        if resolved_at is None:
            keep.append(int(window_id))
            continue
        stamp = str(resolved_at).replace("T", " ")[:19]
        try:
            resolved = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            keep.append(int(window_id))
            continue
        if resolved >= naive_cutoff:
            keep.append(int(window_id))
    return keep


# Small catalog / research tables, copied whole. Anything not listed here or in
# _KEEP_FILTERED is discarded by compaction (that is how raw feeds are dropped).
_KEEP_FULL = ("markets", "windows", "candles", "collector_gaps", "window_snapshots")
_KEEP_FILTERED = ("ticks",)


def _create_attached_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row or not row[0]:
        return False
    ddl = row[0]
    for src in (
        f'CREATE TABLE "{table}"',
        f"CREATE TABLE '{table}'",
        f"CREATE TABLE `{table}`",
        f"CREATE TABLE {table}",
    ):
        if src in ddl:
            conn.execute(ddl.replace(src, f"CREATE TABLE compact.{table}", 1))
            return True
    conn.execute(ddl.replace("CREATE TABLE", "CREATE TABLE compact.", 1))
    return True


def _copy_indexes(conn: sqlite3.Connection, table: str) -> None:
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    for (sql,) in rows:
        if not sql:
            continue
        lowered = sql.lower()
        on_at = lowered.find(" on ")
        if on_at < 0:
            continue
        head = sql[:on_at].rstrip()
        cols_at = sql.find("(", on_at)
        if cols_at < 0:
            continue
        tail = sql[cols_at:]  # "(col, ...) [WHERE ...]" without the table name
        idx_name = head.split()[-1].strip("\"'`[]")
        unique = "UNIQUE " if head.lower().startswith("create unique") else ""
        # SQLite puts the schema on the index name, never on the table:
        # "ON compact.<table>" is a syntax error, which used to drop every index.
        # Names are per database file, so the originals survive the swap.
        try:
            conn.execute(
                f'CREATE {unique}INDEX IF NOT EXISTS compact."{idx_name}" ON "{table}" {tail}'
            )
        except sqlite3.Error as exc:
            log.warning("compact: could not copy index %s: %s", idx_name, exc)


def compact_live_db() -> dict:
    """Rebuild SQLite: windows catalog + recent 1s ticks. No raw feeds. No VACUUM.

    Copies keepers into a new file and swaps. Collector must be stopped.
    Old multi-GB raw tables are discarded with the previous file.
    """
    from pmanalysis.db import session as db_session

    src = Path(db_session.DB_PATH)
    if not src.exists():
        return {"ok": False, "error": "database file missing", "path": str(src)}

    size_before = src.stat().st_size
    dst = src.with_name(src.name + ".compact")
    bak = src.with_name(src.name + ".old")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TICK_RETENTION_HOURS)

    # Migrate first (slim ticks, indicators folded in): the copy below keeps
    # only listed tables, so an un-migrated indicators table would be lost.
    db_session.init_db()
    reset_engine()
    for leftover in (dst, Path(str(dst) + "-wal"), Path(str(dst) + "-shm")):
        leftover.unlink(missing_ok=True)

    src_uri = src.as_posix()
    dst_uri = dst.as_posix().replace("'", "''")
    conn = sqlite3.connect(src_uri, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error as exc:
            log.warning("WAL checkpoint skipped: %s", exc)
        keep_ids = _keep_window_ids(conn, cutoff)
        conn.execute(f"ATTACH DATABASE '{dst_uri}' AS compact")
        created: list[str] = []
        for table in _KEEP_FULL + _KEEP_FILTERED:
            if _create_attached_table(conn, table):
                created.append(table)
        for table in _KEEP_FULL:
            if table in created:
                conn.execute(f"INSERT INTO compact.{table} SELECT * FROM main.{table}")
        conn.execute("CREATE TEMP TABLE keep_windows (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO keep_windows (id) VALUES (?)", [(i,) for i in keep_ids])
        conn.execute(
            """
            INSERT INTO compact.ticks
            SELECT t.* FROM main.ticks AS t
            WHERE t.window_id IN (SELECT id FROM keep_windows)
            """
        )
        for table in _KEEP_FULL + _KEEP_FILTERED:
            _copy_indexes(conn, table)
        tick_count = conn.execute("SELECT COUNT(*) FROM compact.ticks").fetchone()[0]
        window_count = conn.execute("SELECT COUNT(*) FROM compact.windows").fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE compact")
    finally:
        conn.close()

    wal = Path(str(src) + "-wal")
    shm = Path(str(src) + "-shm")
    bak.unlink(missing_ok=True)
    src.replace(bak)
    dst.replace(src)
    wal.unlink(missing_ok=True)
    shm.unlink(missing_ok=True)
    bak.unlink(missing_ok=True)
    reset_engine()
    from pmanalysis.db.session import init_db

    init_db()
    from pmanalysis.datasets.query import clear_archive_fingerprint_cache

    clear_archive_fingerprint_cache()

    size_after = src.stat().st_size if src.exists() else 0
    log.info(
        "Compacted live DB %.1f MB -> %.1f MB (windows=%s ticks=%s keep_ids=%s)",
        size_before / (1024 * 1024),
        size_after / (1024 * 1024),
        window_count,
        tick_count,
        len(keep_ids),
    )
    return {
        "ok": True,
        "path": str(src),
        "size_mb_before": round(size_before / (1024 * 1024), 2),
        "size_mb_after": round(size_after / (1024 * 1024), 2),
        "windows": window_count,
        "ticks": tick_count,
        "windows_with_ticks": len(keep_ids),
        "retention_hours": TICK_RETENTION_HOURS,
    }
