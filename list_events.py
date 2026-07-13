"""Print a full historical event log for every colony.

Runs weight-event detection and sister corroboration over the entire
local data cache (no rolling window). No scoring is performed.

Usage:
    python3 list_events.py
    python3 list_events.py --site 6LR
    python3 list_events.py --site PRT_1 --site DR_WLKS
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.events import corroborate_sister_events, detect_weight_events
from beemon_scoring.quality import filter_quality_issues

PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="List all weight events from the full data history.")
    parser.add_argument("--site", action="append", dest="sites", metavar="SITE_ID",
                        help="Filter to one or more site IDs (e.g. 6LR). Repeatable.")
    args = parser.parse_args()

    hives, colony_sides, _ = load_hive_config(PROJECT_ROOT / "hive_config.py")
    all_raw = load_sensor_readings(PROJECT_ROOT / "local_data" / "dynamodb", hives, colony_sides)
    all_clean, _, _ = filter_quality_issues(all_raw)

    if not all_clean:
        print("No valid sensor readings found.")
        return

    start = min(r.observed_at for r in all_clean)
    end = max(r.observed_at for r in all_clean)
    span_days = (end - start).total_seconds() / 86400

    site_filter = set(args.sites) if args.sites else None

    # Group readings by hive → side
    by_hive: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in all_clean:
        if site_filter and r.hive_id not in site_filter:
            continue
        by_hive[r.hive_id][r.colony_side].append(r)

    # Detect events per hive with sister corroboration
    events_by_colony: dict[str, list] = {}
    for hive_id, sides in by_hive.items():
        raw = {side: detect_weight_events(rdgs) for side, rdgs in sides.items()}
        corroborated = corroborate_sister_events(raw, sides)
        for side, evts in corroborated.items():
            events_by_colony[f"{hive_id}:{side}"] = sorted(evts, key=lambda e: e.observed_at, reverse=True)

    total_events = sum(len(evts) for evts in events_by_colony.values())

    print(f"BeeMon — Full event history")
    print(f"{'=' * 42}")
    print(f"Data span: {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')} ({span_days:.0f} days)")
    print(f"Total events detected: {total_events}")
    print()

    for colony_id in sorted(events_by_colony):
        events = events_by_colony[colony_id]
        print(f"{colony_id}  ({len(events)} event{'s' if len(events) != 1 else ''})")
        if not events:
            print("  —  no events detected")
        else:
            print(f"  {'When':<22}  {'Kind':<12}  {'Delta':>10}  {'Pct':>7}  Before → After")
            print(f"  {'-'*22}  {'-'*12}  {'-'*10}  {'-'*7}  {'-'*22}")
            for ev in events:
                time_str = _fmt_time(ev.observed_at)
                kind_str = ev.kind.ljust(12)
                delta_str = f"{ev.delta_kg:+.3f} kg".rjust(10)
                pct_str = f"{ev.pct_change:+.1f}%".rjust(7)
                range_str = f"{ev.before_kg:.3f} → {ev.after_kg:.3f} kg"
                print(f"  {time_str:<22}  {kind_str}  {delta_str}  {pct_str}  {range_str}")
        print()


def _fmt_time(dt: datetime) -> str:
    offset = dt.strftime("%z")           # e.g. "-0400"
    tz_str = f"{offset[:3]}:{offset[3:]}"  # → "-04:00"
    return dt.strftime("%Y-%m-%d %H:%M") + " " + tz_str


if __name__ == "__main__":
    main()
