from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh BeeMon data caches and run colony scoring.")
    parser.add_argument("--days", type=int, default=7, help="Number of recent days to fetch and score.")
    parser.add_argument("--skip-fetch", action="store_true", help="Only score cached local data; do not call live services.")
    parser.add_argument("--json-output", type=Path, default=Path("output/scoring.json"), help="JSON output path.")
    args = parser.parse_args()

    if not args.skip_fetch:
        run([sys.executable, "fetch_dynamodb.py", "--days", str(args.days)])
        run([sys.executable, "fetch_openmeteo.py", "--days", str(args.days)])

    run([
        sys.executable,
        "run_scoring.py",
        "--window-days",
        str(args.days),
        "--format",
        "json",
        "--output",
        str(args.json_output),
    ])
    run([sys.executable, "run_scoring.py", "--window-days", str(args.days)])


def run(command: list[str]) -> None:
    print(f"\n$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


if __name__ == "__main__":
    main()
