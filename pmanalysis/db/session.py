"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from pmanalysis.config import DB_PATH, MARKET_SLUGS, ensure_data_dirs
from pmanalysis.db.models import Base, Market

_engine = None
_SessionLocal = None


def _sqlite_url() -> str:
    ensure_data_dirs()
    return f"sqlite:///{DB_PATH.as_posix()}"


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            _sqlite_url(),
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    with get_session() as session:
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


def db_health() -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    size_mb = round(DB_PATH.stat().st_size / (1024 * 1024), 2) if DB_PATH.exists() else 0.0
    return {"path": str(DB_PATH), "size_mb": size_mb, "ok": True}
