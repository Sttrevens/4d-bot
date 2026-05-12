#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.harness.benchmark import run_benchmark_suite


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic Hermes benchmark scenarios.")
    parser.add_argument("suite", type=Path, help="Scenario JSON file or directory of JSON files.")
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--live", action="store_true", help="Run live integrations instead of deterministic replay.")
    return parser


async def _main() -> int:
    args = _parser().parse_args()
    summary = await run_benchmark_suite(args.suite, args.output_dir, live=args.live)
    print(f"{summary['suite']}: {summary['passed']}/{summary['total']} passed")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
