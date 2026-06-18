from __future__ import annotations

import argparse
from pathlib import Path

from beemon_scoring.reporting import build_text_report, scores_to_json
from beemon_scoring.scoring import build_scores


def main() -> None:
    parser = argparse.ArgumentParser(description="Score beehive colonies relative to regional peers.")
    parser.add_argument("--window-days", type=int, default=None, help="Rolling window size. Defaults to hive_config.py.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    parser.add_argument("--output", type=Path, default=None, help="Optional output file.")
    parser.add_argument(
        "--sensor-dir",
        type=Path,
        default=None,
        help="Local DynamoDB CSV cache. Defaults to local_data/dynamodb.",
    )
    parser.add_argument(
        "--weather-dir",
        type=Path,
        default=None,
        help="Local Open-Meteo CSV cache. Defaults to local_data/openmeteo.",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    scores, metadata = build_scores(
        project_root,
        window_days=args.window_days,
        sensor_dir=args.sensor_dir,
        weather_dir=args.weather_dir,
    )
    rendered = scores_to_json(scores, metadata) if args.format == "json" else build_text_report(scores, metadata)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
