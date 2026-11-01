"""CLI entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from pmanalysis.collector.service import CollectorService
from pmanalysis.config import TICK_RETENTION_HOURS, WEB_HOST, WEB_PORT, ensure_data_dirs
from pmanalysis.web.app import run_web

log = logging.getLogger(__name__)


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx/httpcore log an INFO line per HTTP request; at the REST /book poll
    # rate this alone filled the PM2 error log to multiple GB. Keep it quiet
    # unless -v is passed.
    if not verbose:
        for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _cmd_collector() -> None:
    ensure_data_dirs()
    service = CollectorService()
    try:
        asyncio.run(service.start())
    except KeyboardInterrupt:
        asyncio.run(service.stop())
        print("Collector stopped.")


def _cmd_start() -> None:
    log.info("Starting collector + web in one process (WebSocket live stream)")
    log.info("Listening on %s:%s", WEB_HOST, WEB_PORT)
    if WEB_HOST in ("0.0.0.0", "::"):
        log.info("Local: http://127.0.0.1:%s  |  Remote: http://<your-ip>:%s", WEB_PORT, WEB_PORT)
    else:
        log.info("Open http://%s:%s", WEB_HOST, WEB_PORT)
    run_web(embed_collector=True)


def _cmd_purge_broken(yes: bool, dry_run: bool, strict: bool) -> None:
    from pmanalysis.db.cleanup import purge_broken_windows

    if dry_run:
        result = purge_broken_windows(dry_run=True, strict=strict)
        print("Broken windows (dry run, no changes):")
        print(f"  strict:     {result.get('strict')}")
        print(f"  candidates: {result.get('candidates')}")
        for key, count in sorted((result.get("reasons") or {}).items()):
            if count:
                print(f"  {key}: {count}")
        return

    if not yes:
        print("Stop the collector first. This permanently deletes closed windows that are:")
        if strict:
            print("  - missing an outcome (UP/DOWN), or")
            print("  - incomplete 1s tick series (same rules as /v1 dataset `complete`)")
            print("  WARNING: strict mode can remove almost all history if feeds were gappy.")
        else:
            print("  - missing an outcome (UP/DOWN), or")
            print("  - marked archived but parquet file is missing, or")
            print("  - have zero tick rows in SQLite and parquet")
        print("Use --strict to also drop gappy windows (dataset `complete` rules).")
        print("Parquet archives and catalog rows for those windows are removed.")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            return

    result = purge_broken_windows(dry_run=False, strict=strict)
    print("Broken window purge complete.")
    print(f"  windows removed:   {result.get('windows_removed')}")
    print(f"  parquet removed:   {result.get('parquet_removed')}")
    print(f"  ticks removed:     {result.get('ticks_removed')}")
    for key, count in sorted((result.get("reasons") or {}).items()):
        if count:
            print(f"  {key}: {count}")


def _cmd_prune_db(yes: bool, vacuum: bool) -> None:
    from pmanalysis.db.cleanup import compact_live_db, vacuum_db
    from pmanalysis.db.session import db_health, init_db

    if not yes:
        print("Stop the collector first. This rebuilds SQLite:")
        print("  - keeps windows catalog (names, outcomes)")
        print(f"  - keeps 1s ticks from the last {TICK_RETENTION_HOURS}h")
        print("  - drops raw Binance / Oracle / CLOB tables")
        print("  - keeps parquet archives")
        if vacuum:
            print("  - VACUUM (usually unnecessary after compact)")
        answer = input("Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            return

    result = compact_live_db()
    if not result.get("ok"):
        print(f"Compact failed: {result.get('error')}")
        return
    init_db()
    print("Live database compacted.")
    print(f"  windows: {result.get('windows')}")
    print(f"  ticks:   {result.get('ticks')} (last {result.get('retention_hours')}h)")
    print(f"  size:    {result.get('size_mb_before')} MB -> {result.get('size_mb_after')} MB")
    if vacuum:
        print("VACUUM…")
        vacuum_db()
        print("VACUUM complete.")
    health = db_health()
    print(f"  db: {health.get('path')} ({health.get('size_mb')} MB)")


def _cmd_backfill_features(limit: int | None, batch: int = 200) -> None:
    """Compute window_snapshots / window features for resolved windows lacking them."""
    from pmanalysis.collector.resolution import infer_outcome
    from pmanalysis.db.features import compute_window_features, windows_missing_features
    from pmanalysis.db.session import get_session, init_db

    init_db()
    done = skipped = 0
    after_id = 0
    while limit is None or done + skipped < limit:
        size = batch if limit is None else min(batch, limit - done - skipped)
        with get_session() as session:
            windows = windows_missing_features(session, after_id=after_id, limit=size)
            if not windows:
                break
            for window in windows:
                after_id = window.id
                features = compute_window_features(session, window)
                if features is None or not features.snapshots:
                    skipped += 1  # no 1s data left for this window
                    continue
                if window.inferred_outcome is None:
                    window.inferred_outcome = infer_outcome(window)
                # Commit per window: safe to run while the collector is writing.
                session.commit()
                done += 1
        print(f"  {done} windows backfilled, {skipped} without 1s data", flush=True)
    print(f"Backfill complete: {done} windows, {skipped} skipped.")


def _check_deps() -> None:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        print("ERROR: Dependencies not installed.")
        print("Run once:")
        print("  pip install -e .")
        print("Or:  .\\scripts\\setup.ps1")
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    _check_deps()
    parser = argparse.ArgumentParser(
        description="Polymarket crypto analysis tool",
        epilog="Quick start: python -m pmanalysis start",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collector", help="Run data collector only")
    sub.add_parser("web", help="Run web dashboard only (DB sync mode)")
    sub.add_parser("start", help="Run collector + web together (recommended)")
    prune_p = sub.add_parser(
        "prune-db",
        help="Drop raw feeds and archived 1s ticks older than retention; keep windows + parquet",
    )
    prune_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    prune_p.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM SQLite afterwards so the file shrinks on disk",
    )
    broken_p = sub.add_parser(
        "purge-broken-windows",
        help="Delete closed windows with incomplete ticks or missing outcomes",
    )
    broken_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    broken_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many windows would be removed without deleting",
    )
    broken_p.add_argument(
        "--strict",
        action="store_true",
        help="Also drop windows failing dataset complete rules (95%% fill, gaps)",
    )

    backfill_p = sub.add_parser(
        "backfill-features",
        help="Compute research snapshots (calibration/regimes/backtest) for archived windows",
    )
    backfill_p.add_argument("--limit", type=int, default=None, help="Max windows to process")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    ensure_data_dirs()

    if args.command == "collector":
        _cmd_collector()
    elif args.command == "web":
        run_web(embed_collector=False)
    elif args.command == "start":
        _cmd_start()
    elif args.command == "prune-db":
        _cmd_prune_db(yes=args.yes, vacuum=args.vacuum)
    elif args.command == "purge-broken-windows":
        _cmd_purge_broken(yes=args.yes, dry_run=args.dry_run, strict=args.strict)
    elif args.command == "backfill-features":
        _cmd_backfill_features(limit=args.limit)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
