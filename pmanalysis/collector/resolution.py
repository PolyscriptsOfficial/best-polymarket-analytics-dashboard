"""Outcome resolution: official Gamma winner vs our own TWAP inference.

Every finalized window records ``inferred_outcome`` (final 60s TWAP vs the
window-open TWAP). When Gamma publishes a winner it becomes ``outcome`` with
``outcome_source="gamma"``; if it disagrees with the inference the window is
flagged ``outcome_mismatch``. When Gamma stays silent past the resolution
deadline the inference is used (``outcome_source="inferred"``) and re-checked
against Gamma for RESOLUTION_RECHECK_HOURS.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from pmanalysis.db.models import Window


def _twap_inference(window: Window) -> str | None:
    if window.final_twap is None or window.twap_initial is None:
        return None
    # Ties resolve UP ("greater than or equal to").
    return "UP" if window.final_twap >= window.twap_initial else "DOWN"


def infer_outcome(window: Window) -> str | None:
    twap = _twap_inference(window)
    if twap is not None:
        return twap
    if window.final_oracle_price is not None and window.strike_price is not None:
        # Spot fallback when a TWAP was not captured (collector down at a boundary).
        return "UP" if window.final_oracle_price >= window.strike_price else "DOWN"
    return None


def apply_resolution(window: Window, official: str | None) -> bool:
    """Set outcome fields from Gamma's winner (or the inference). True if outcome changed."""
    before = window.outcome
    inferred = infer_outcome(window)
    window.inferred_outcome = inferred
    if official:
        window.outcome = official
        window.outcome_source = "gamma"
        # Only the settlement-aligned TWAP inference can disagree meaningfully;
        # the spot fallback is a rough guess and would raise false alarms.
        twap = _twap_inference(window)
        window.outcome_mismatch = twap is not None and twap != official
    elif inferred:
        window.outcome = inferred
        window.outcome_source = "inferred"
        window.outcome_mismatch = False
    return window.outcome != before


def refresh_inference(window: Window) -> bool:
    """Re-derive inference + mismatch after TWAP fields were filled in later.

    Feature extraction can recover ``twap_initial`` / ``final_twap`` from 1s rows
    when the live capture missed them. Legacy rows (no ``outcome_source``) are
    left alone: their outcome's origin is unknown. True if ``outcome`` changed.
    """
    if window.outcome_source == "gamma":
        return apply_resolution(window, window.outcome)
    if window.outcome_source == "inferred":
        return apply_resolution(window, None)
    return False


def windows_to_recheck(session: Session, now_ts: int, max_age_sec: int, limit: int = 20) -> list[Window]:
    """Inferred outcomes still young enough that Gamma may publish a winner."""
    return (
        session.query(Window)
        .filter(Window.outcome_source == "inferred", Window.end_ts >= now_ts - max_age_sec)
        .order_by(Window.end_ts.desc())
        .limit(limit)
        .all()
    )


def mismatch_summary(session: Session, limit: int = 20) -> dict:
    """Counts by outcome source plus the latest mismatching windows (for /status)."""
    counts = dict(
        session.query(Window.outcome_source, func.count(Window.id))
        .filter(Window.outcome_source.isnot(None))
        .group_by(Window.outcome_source)
        .all()
    )
    mismatches = (
        session.query(Window)
        .filter(Window.outcome_mismatch.is_(True))
        .order_by(Window.end_ts.desc())
        .limit(limit)
        .all()
    )
    total_mismatch = session.query(func.count(Window.id)).filter(Window.outcome_mismatch.is_(True)).scalar()
    return {
        "gamma": int(counts.get("gamma", 0)),
        "inferred": int(counts.get("inferred", 0)),
        "mismatches": int(total_mismatch or 0),
        "recent_mismatches": [
            {
                "slug": w.slug,
                "asset": w.asset,
                "interval": w.interval,
                "end_ts": w.end_ts,
                "outcome": w.outcome,
                "inferred_outcome": w.inferred_outcome,
                "twap_initial": w.twap_initial,
                "final_twap": w.final_twap,
            }
            for w in mismatches
        ],
    }
