"""Persist feed outages into collector_gaps (one open row per source)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from pmanalysis.collector.health import is_gap_status
from pmanalysis.db.models import CollectorGap


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GapTracker:
    """Open/close CollectorGap rows as feed health transitions.

    Safe to call from the serialized DB worker. In-memory `_open_ids` is the
    source of truth between ticks so we do not insert duplicate open rows.
    """

    def __init__(self) -> None:
        self._open_ids: dict[str, int] = {}

    def hydrate(self, session: Session) -> None:
        rows = (
            session.query(CollectorGap)
            .filter(CollectorGap.ended_at.is_(None))
            .all()
        )
        self._open_ids = {row.source: row.id for row in rows}

    def reconcile(self, session: Session, feeds: list[dict]) -> list[tuple[str, str]]:
        """Open/close gap rows; return (source, message) for gaps opened now."""
        opened: list[tuple[str, str]] = []
        for feed in feeds:
            source = feed["source"]
            message = _gap_message(feed)
            open_id = self._open_ids.get(source)
            if is_gap_status(feed["status"]):
                if open_id is None:
                    gap = CollectorGap(
                        source=source,
                        started_at=_utcnow(),
                        message=message,
                    )
                    session.add(gap)
                    session.flush()
                    self._open_ids[source] = gap.id
                    opened.append((source, message))
                else:
                    row = session.get(CollectorGap, open_id)
                    if row is not None and row.message != message:
                        row.message = message
            elif open_id is not None:
                row = session.get(CollectorGap, open_id)
                if row is not None and row.ended_at is None:
                    row.ended_at = _utcnow()
                self._open_ids.pop(source, None)
        session.flush()
        return opened


def _gap_message(feed: dict) -> str:
    status = feed.get("status") or "down"
    lag = feed.get("lag_sec")
    threshold = feed.get("threshold_sec")
    if status == "down":
        if lag is None:
            return "disconnected (no events)"
        return f"disconnected (last event {lag:.1f}s ago)"
    if lag is None:
        return f"{status}"
    return f"{status}: lag {lag:.1f}s (threshold {threshold:g}s)"
