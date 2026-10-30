"""Persist per-window research features (window columns + window_snapshots)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import exists
from sqlalchemy.orm import Session

from pmanalysis.analytics.features import WindowFeatures, extract_features_frame
from pmanalysis.collector.resolution import refresh_inference
from pmanalysis.db.models import Window, WindowSnapshot
from pmanalysis.export.parquet import window_frame


def save_window_features(session: Session, window: Window, features: WindowFeatures) -> None:
    # Live captures win over values re-derived from 1s rows.
    if window.twap_initial is None:
        window.twap_initial = features.twap_initial
    if window.final_twap is None:
        window.final_twap = features.final_twap
    window.realized_vol = features.realized_vol
    window.open_move = features.open_move
    session.query(WindowSnapshot).filter_by(window_id=window.id).delete(synchronize_session=False)
    for t_minus, snap in features.snapshots.items():
        session.add(
            WindowSnapshot(
                window_id=window.id,
                asset=window.asset,
                interval=window.interval,
                start_ts=window.start_ts,
                t_minus=t_minus,
                **snap,
            )
        )
    session.add(window)
    # Sessions run with autoflush off; flush so the next window's boundary
    # lookup (boundary_twap) sees this window's final TWAP.
    session.flush()


def boundary_twap(session: Session, window: Window) -> float | None:
    """Opening TWAP from the previous window's close (they share the boundary)."""
    row = (
        session.query(Window.final_twap)
        .filter(
            Window.asset == window.asset,
            Window.interval == window.interval,
            Window.end_ts == window.start_ts,
            Window.final_twap.isnot(None),
        )
        .first()
    )
    return row[0] if row else None


def compute_window_features(session: Session, window: Window) -> WindowFeatures | None:
    frame = window_frame(session, window)
    if frame.empty:
        return None
    features = extract_features_frame(
        frame,
        interval=window.interval,
        start_ts=window.start_ts,
        end_ts=window.end_ts,
        twap_initial=window.twap_initial or boundary_twap(session, window),
    )
    save_window_features(session, window, features)
    if refresh_inference(window):
        # Bump resolved_at so dataset fingerprints (max resolved_at) rebuild.
        window.resolved_at = datetime.now(timezone.utc)
    return features


def windows_missing_features(
    session: Session, *, after_id: int = 0, limit: int | None = None
) -> list[Window]:
    """Resolved windows without checkpoint snapshots, in id order after ``after_id``."""
    query = (
        session.query(Window)
        .filter(Window.resolved_at.isnot(None), Window.id > after_id)
        .filter(~exists().where(WindowSnapshot.window_id == Window.id))
        .order_by(Window.id.asc())
    )
    if limit:
        query = query.limit(limit)
    return query.all()
