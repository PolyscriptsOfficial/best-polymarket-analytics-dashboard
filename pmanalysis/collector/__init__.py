"""Collector package."""

from __future__ import annotations

__all__ = ["CollectorService"]


def __getattr__(name: str):
    if name == "CollectorService":
        from pmanalysis.collector.service import CollectorService

        return CollectorService
    raise AttributeError(name)
