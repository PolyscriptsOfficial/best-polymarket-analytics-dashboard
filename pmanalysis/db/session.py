"""Database engine and session helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from pmanalysis.config import DB_PATH, MARKET_SLUGS, ensure_data_dirs
from pmanalysis.db.models import Base, Market, Tick, Window, WindowSnapshot
from pmanalysis.db.tick_fields import STORED_TICK_COLUMNS

_engine = None
_SessionLocal = None
T = TypeVar("T")


def _sqlite_url() -> str:
    ensure_data_dirs()
    return f"sqlite:///{DB_PATH.as_posix()}"


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        # NullPool: open/close per session — avoids SQLite pool exhaustion under async load.
        _engine = create_engine(
            _sqlite_url(),
            connect_args={"check_same_thread": False, "timeout": 60},
            poolclass=NullPool,
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA cache_size=-65536")  # 64 MiB page cache
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()

        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


# Columns added after the first deploy. SQLite has no ADD COLUMN IF NOT EXISTS,
# so each is added once when missing. New tables come from create_all().
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "windows": (
        ("opening_oracle_price", "FLOAT"),
        ("twap_initial", "FLOAT"),
        ("final_twap", "FLOAT"),
        ("outcome_source", "VARCHAR(16)"),
        ("inferred_outcome", "VARCHAR(8)"),
        ("outcome_mismatch", "BOOLEAN DEFAULT 0"),
        ("realized_vol", "FLOAT"),
        ("open_move", "FLOAT"),
    ),
    "ticks": (
        ("binance_price", "FLOAT"),
        ("twap_oracle", "FLOAT"),
        ("yes_ask_size", "FLOAT"),
        ("yes_bid_size", "FLOAT"),
        ("no_ask_size", "FLOAT"),
        ("no_bid_size", "FLOAT"),
        ("yes_ask_depth", "FLOAT"),
        ("yes_bid_depth", "FLOAT"),
        ("no_ask_depth", "FLOAT"),
        ("no_bid_depth", "FLOAT"),
        ("yes_last_trade", "FLOAT"),
        ("no_last_trade", "FLOAT"),
        ("traded_usd", "FLOAT"),
        ("fair_yes", "FLOAT"),
    ),
}

# Columns the pre-2026-10-31 ticks table stored although they are derivable
# (see db/tick_fields.py); their presence triggers the one-time rebuild.
_LEGACY_TICK_COLUMNS = (
    "yes_mid",
    "no_mid",
    "combined_ask",
    "yes_spread",
    "secs_remaining",
    "dist_from_strike",
    "binance_perp_price",
)


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _migrate_tick_layout(engine) -> None:
    """One-time rebuild of ``ticks`` into the slim layout and drop ``indicators``.

    The old schema wrote two rows per market-second (ticks + indicators) and
    stored ~20 derivable columns. TWAP and fair value move from indicators
    into ticks; everything else derivable is dropped. Runs in one transaction,
    so an interrupted start leaves the old tables untouched.
    """
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        if "ticks" not in tables:
            return
        legacy_cols = _table_columns(conn, "ticks")
        has_indicators = "indicators" in tables
        if not has_indicators and not legacy_cols.intersection(_LEGACY_TICK_COLUMNS):
            return

        conn.execute(text("DROP INDEX IF EXISTS ix_ticks_window_ts"))
        conn.execute(text("ALTER TABLE ticks RENAME TO ticks_legacy"))
        Tick.__table__.create(conn)

        from_indicators = ("twap_oracle", "fair_yes") if has_indicators else ()
        columns = ["id", "window_id", "ts_ms"]
        selects = ["t.id", "t.window_id", "t.ts_ms"]
        for name in STORED_TICK_COLUMNS:
            if name in from_indicators:
                columns.append(name)
                selects.append(f"i.{name}")
            elif name in legacy_cols:
                columns.append(name)
                selects.append(f"t.{name}")
        join = ""
        if has_indicators:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_indicators_window_ts "
                    "ON indicators (window_id, ts_ms)"
                )
            )
            # GROUP BY collapses duplicate indicator rows so ticks never multiply.
            join = (
                " LEFT JOIN (SELECT window_id, ts_ms, MAX(twap_oracle) AS twap_oracle,"
                " MAX(fair_yes) AS fair_yes FROM indicators GROUP BY window_id, ts_ms) AS i"
                " ON i.window_id = t.window_id AND i.ts_ms = t.ts_ms"
            )
        conn.execute(
            text(
                f"INSERT INTO ticks ({', '.join(columns)}) "
                f"SELECT {', '.join(selects)} FROM ticks_legacy AS t{join}"
            )
        )
        conn.execute(text("DROP TABLE ticks_legacy"))
        if has_indicators:
            conn.execute(text("DROP TABLE indicators"))


def _migrate_columns(engine) -> None:
    """Add columns introduced after initial deploy."""
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _ensure_indexes(engine) -> None:
    """Create every model-declared index that is missing.

    create_all() skips existing tables *including their indexes*, so indexes
    added to a model later (or lost when a table is rebuilt) would otherwise
    never appear. Without ix_ticks_window_ts every per-window tick read is a
    full table scan.
    """
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                index.create(conn, checkfirst=True)


def _purge_retired_intervals(session: Session) -> None:
    """Remove markets/intervals no longer tracked (e.g. retired 1h)."""
    active = {interval for _, interval, _ in MARKET_SLUGS}
    retired = (
        session.query(Market.interval)
        .distinct()
        .all()
    )
    for (interval,) in retired:
        if interval in active:
            continue
        window_ids = [
            row[0]
            for row in session.query(Window.id).filter(Window.interval == interval).all()
        ]
        if window_ids:
            session.query(Tick).filter(Tick.window_id.in_(window_ids)).delete(
                synchronize_session=False
            )
            session.query(WindowSnapshot).filter(
                WindowSnapshot.window_id.in_(window_ids)
            ).delete(synchronize_session=False)
        session.query(Window).filter(Window.interval == interval).delete()
        session.query(Market).filter(Market.interval == interval).delete()


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    _migrate_tick_layout(engine)
    _migrate_columns(engine)
    _ensure_indexes(engine)
    with get_session() as session:
        _purge_retired_intervals(session)
        for asset, interval, base_slug in MARKET_SLUGS:
            exists = session.query(Market).filter_by(base_slug=base_slug).first()
            if not exists:
                session.add(Market(asset=asset, interval=interval, base_slug=base_slug))
        session.commit()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def run_db(fn: Callable[[], T]) -> T:
    """Run blocking DB work off the asyncio event loop."""
    return await asyncio.to_thread(fn)


def reset_engine() -> None:
    """Drop the cached SQLAlchemy engine so the next call reopens DB_PATH."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def db_health() -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0.0
    return {"path": str(DB_PATH), "size_mb": size_mb, "ok": True}
