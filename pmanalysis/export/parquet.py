"""Export resolved windows to Parquet and CSV."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pmanalysis import config
from pmanalysis.db.models import Tick, Window
from pmanalysis.db.tick_fields import STORED_TICK_COLUMNS, with_derived


def window_frame(session: Session, window: Window) -> pd.DataFrame:
    """Full 1s history of a window: SQLite while hot, else its parquet archive.

    Hot frames hold ``ts_ms`` + STORED_TICK_COLUMNS; archives written before the
    slim layout also carry derived columns. Readers derive what they need with
    db/tick_fields.py either way.
    """
    rows = session.execute(
        select(Tick.ts_ms, *(getattr(Tick, name) for name in STORED_TICK_COLUMNS))
        .where(Tick.window_id == window.id)
        .order_by(Tick.ts_ms)
    ).all()
    if not rows:
        archived = load_window_frame(window)
        return archived if archived is not None else pd.DataFrame()
    # float64 even when a column is all-null, so every archive has one schema.
    return pd.DataFrame(rows, columns=("ts_ms", *STORED_TICK_COLUMNS)).astype(
        {name: "float64" for name in STORED_TICK_COLUMNS}
    )


def export_window_parquet(session: Session, window: Window) -> Path:
    frame = window_frame(session, window)
    out_dir = config.ARCHIVE_DIR / window.asset.lower() / window.interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{window.slug}.parquet"
    frame.to_parquet(out_path, index=False, compression="zstd")
    window.archived = True
    session.add(window)
    return out_path


def export_window_csv(session: Session, window: Window) -> Path:
    """Human-facing export: stored columns plus the derived ones (mids, spread, ...)."""
    frame = window_frame(session, window)
    if not frame.empty:
        rows = with_derived(
            frame.to_dict(orient="records"),
            end_ts=window.end_ts,
            strike_price=window.strike_price,
            twap_initial=window.twap_initial,
        )
        frame = pd.DataFrame(rows)
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.EXPORT_DIR / f"{window.slug}.csv"
    frame.to_csv(out_path, index=False)
    return out_path


def archive_path_for(asset: str, interval: str, slug: str) -> Path:
    return config.ARCHIVE_DIR / asset.lower() / interval / f"{slug}.parquet"


def load_window_frame(window: Window) -> pd.DataFrame | None:
    """Read a finalized window's full tick history back from its parquet archive.

    Used as the backtest data source once live SQLite ticks have been
    pruned for storage/perf reasons (see db/cleanup.py retention job) — the archive
    is written once per window at finalize time and never touched again.
    """
    path = archive_path_for(window.asset, window.interval, window.slug)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None
