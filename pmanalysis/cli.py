"""CLI entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from pmanalysis.collector.service import CollectorService
from pmanalysis.config import WEB_HOST, WEB_PORT, ensure_data_dirs
from pmanalysis.web.app import run_web

log = logging.getLogger(__name__)


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


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
    log.info("Open http://%s:%s", WEB_HOST, WEB_PORT)
    run_web(embed_collector=True)


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

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    ensure_data_dirs()

    if args.command == "collector":
        _cmd_collector()
    elif args.command == "web":
        run_web(embed_collector=False)
    elif args.command == "start":
        _cmd_start()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
