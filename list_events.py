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
from datetime import datetime
from pathlib import Path

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.events import detect_site_events

PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="List all weight events from the full data history.")
    parser.add_argument("--site", action="append", dest="sites", metavar="SITE_ID",
                        help="Filter to one or more site IDs (e.g. 6LR). Repeatable.")
    args = parser.parse_args()

    hives, colony_sides, _ = load_hive_config(PROJECT_ROOT / "hive_config.py")
    all_raw = load_sensor_readings(PROJECT_ROOT / "local_data" / "dynamodb", hives, colony_sides)

    if not all_raw:
        print("No sensor readings found.")
        return

    start = min(r.observed_at for r in all_raw)
    end = max(r.observed_at for r in all_raw)
    span_days = (end - start).total_seconds() / 86400

    site_filter = set(args.sites) if args.sites else None

    # Detect events exactly as production does: detect_site_events on the
    # pre-filter readings (the single source of truth used by build_scores), not
    # a second pass over quality-filtered data.
    detected = detect_site_events(all_raw)
    events_by_colony: dict[str, list] = {}
    for colony_id, evts in detected.items():
        if site_filter and colony_id.split(":")[0] not in site_filter:
            continue
        events_by_colony[colony_id] = sorted(evts, key=lambda e: e.observed_at, reverse=True)

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
