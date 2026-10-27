"""Remove corrupt ticks; keep SQLite bounded after parquet archive."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, text, tuple_

from pmanalysis.config import FEED_RECORD_ENABLED, TICK_RETENTION_HOURS
from pmanalysis.db.models import BinanceTrade, ClobQuote, Indicator, OracleTick, Tick, Window
from pmanalysis.db.session import get_engine, get_session, reset_engine

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
            return {
                "ticks_removed": 0,
                "indicators_removed": 0,
                "windows_affected": 0,
            }

        tick_ids = [row[0] for row in bad_rows]
        pairs = list({(row[1], row[2]) for row in bad_rows})
        windows_affected = len({row[1] for row in bad_rows})

        indicators_removed = (
            session.query(Indicator)
            .filter(tuple_(Indicator.window_id, Indicator.ts_ms).in_(pairs))
            .delete(synchronize_session=False)
        )
        ticks_removed = (
            session.query(Tick)
            .filter(Tick.id.in_(tick_ids))
            .delete(synchronize_session=False)
        )
        session.commit()

    return {
        "ticks_removed": ticks_removed,
        "indicators_removed": indicators_removed,
        "windows_affected": windows_affected,
    }


def purge_archived_ticks(*, retention_hours: int | None = None, batch_size: int = 25) -> dict:
    """Delete Tick/Indicator rows for windows already archived to parquet.

    Only windows that are (a) marked archived=True (parquet file written by
    export_window_parquet) and (b) resolved more than `retention_hours` ago are
    touched. The windows catalog is kept. Deletes run in small batches so the
    live collector is not locked for long.
    """
    hours = TICK_RETENTION_HOURS if retention_hours is None else retention_hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_session() as session:
        window_ids = [
            row[0]
            for row in session.query(Window.id)
            .filter(Window.archived.is_(True), Window.resolved_at < cutoff)
            .all()
        ]

    ticks_removed = 0
    indicators_removed = 0
    windows_affected = 0
    for offset in range(0, len(window_ids), batch_size):
        batch = window_ids[offset : offset + batch_size]
        with get_session() as session:
            n_ind = (
                session.query(Indicator)
                .filter(Indicator.window_id.in_(batch))
                .delete(synchronize_session=False)
            )
            n_tick = (
                session.query(Tick)
                .filter(Tick.window_id.in_(batch))
                .delete(synchronize_session=False)
            )
            session.commit()
        indicators_removed += n_ind
        ticks_removed += n_tick
        if n_tick or n_ind:
            windows_affected += len(batch)

    if ticks_removed or indicators_removed:
        log.info(
            "Retention purge: %d ticks, %d indicators removed across %d windows (archived, >%dh old)",
            ticks_removed,
            indicators_removed,
            windows_affected,
            hours,
        )

    return {
        "ticks_removed": ticks_removed,
        "indicators_removed": indicators_removed,
        "windows_affected": windows_affected,
        "retention_hours": hours,
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


def run_retention(*, rebuild_raw: bool = False) -> dict:
    """One retention pass: prune archived ticks, raw feeds, then checkpoint WAL."""
    ticks = purge_archived_ticks()
    feeds = purge_raw_feeds(rebuild=rebuild_raw)
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


_KEEP_FULL = ("markets", "windows", "candles", "collector_gaps")
_KEEP_FILTERED = ("ticks", "indicators")


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
        tail = sql[on_at + 4 :].lstrip()
        idx_name = head.split()[-1].strip("\"'`[]")
        try:
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}_c" ON compact.{table} {tail}')
        except sqlite3.Error:
            continue


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
        conn.execute(
            """
            INSERT INTO compact.indicators
            SELECT i.* FROM main.indicators AS i
            WHERE i.window_id IN (SELECT id FROM keep_windows)
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
