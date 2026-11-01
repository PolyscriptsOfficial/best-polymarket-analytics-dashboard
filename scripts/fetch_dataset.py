"""Download a history dataset zip from a running collector.

  python scripts/fetch_dataset.py --start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pmanalysis.datasets.client import fetch_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch a period dataset zip from /v1/datasets")
    parser.add_argument("--url", default=os.getenv("PM_DATASET_URL", "http://127.0.0.1:8420"))
    parser.add_argument("--start", required=True, help="Inclusive UTC instant, e.g. 2026-10-01T00:00:00Z")
    parser.add_argument("--end", required=True, help="Exclusive UTC instant")
    parser.add_argument("--asset", action="append", dest="assets", required=True)
    parser.add_argument("--interval", action="append", dest="intervals", required=True)
    parser.add_argument("--out", type=Path, default=Path("dataset.zip"))
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("PM_DATASET_TIMEOUT", "3600")),
        help="Seconds to wait for build + download (default 3600)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="Seconds between status polls (default 1)",
    )
    args = parser.parse_args()
    path = fetch_dataset(
        base_url=args.url,
        start=args.start,
        end=args.end,
        assets=[item.upper() for item in args.assets],
        intervals=args.intervals,
        dest=args.out,
        timeout_sec=args.timeout,
        poll_sec=args.poll,
    )
    print(path)


if __name__ == "__main__":
    main()
