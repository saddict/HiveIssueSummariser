"""Diagnostic spike: MAD-based weight event detection validation.

Prints per-colony MAD stats and robust-z scores for every large step,
with explicit callouts for the 6LR 2026-07-07 21:00 UTC harvest.

Usage:
    python3 spike_mad_events.py
    python3 spike_mad_events.py --window-days 14
"""
from __future__ import annotations

import argparse
import datetime
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.events import (
    MAX_EVENT_INTERVAL_HOURS,
    MAD_SENSITIVITY_K,
    _MIN_MAD_DELTAS,
    _MAD_EPSILON,
    _robust_step_stats,
    detect_weight_events,
)
from beemon_scoring.quality import filter_quality_issues

UTC = ZoneInfo("UTC")

HARVEST_TS = datetime.datetime(2026, 7, 7, 21, 0, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parent


def _robust_z_for_step(ordered, index, median_delta, mad):
    """Return robust-z of the step ending at ordered[index], or None if skipped."""
    prev = ordered[index - 1]
    curr = ordered[index]
    if prev.weight_kg <= 0 or curr.weight_kg <= 0:
        return None
    elapsed_hours = (curr.observed_at - prev.observed_at).total_seconds() / 3600
    if elapsed_hours <= 0 or elapsed_hours > MAX_EVENT_INTERVAL_HOURS:
        return None
    delta_per_hour = (curr.weight_kg - prev.weight_kg) / elapsed_hours
    return (delta_per_hour - median_delta) / mad


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=7)
    args = parser.parse_args()

    hives, colony_sides, settings = load_hive_config(PROJECT_ROOT / "hive_config.py")
    sensor_dir = PROJECT_ROOT / "local_data" / "dynamodb"
    all_raw = load_sensor_readings(sensor_dir, hives, colony_sides)
    all_clean, _, _ = filter_quality_issues(all_raw)

    # Apply window the same way scoring.py does
    end_at = max(r.observed_at for r in all_clean)
    start_at = end_at - datetime.timedelta(days=args.window_days)
    readings_in_window = [r for r in all_clean if r.observed_at >= start_at]

    by_colony: dict[str, list] = defaultdict(list)
    for r in readings_in_window:
        by_colony[r.colony_id].append(r)

    print(f"\nMAD weight-event spike  (window={args.window_days}d, k={MAD_SENSITIVITY_K})\n")
    header = f"{'Colony':<14} {'n_deltas':>8} {'median Δkg/h':>12} {'MAD':>8} {'use_MAD':>7}"
    print(header)
    print("-" * len(header))

    all_steps: dict[str, list[tuple]] = {}

    for colony_id in sorted(by_colony):
        readings = sorted(by_colony[colony_id], key=lambda r: r.timestamp)
        median_delta, mad, n_usable = _robust_step_stats(readings)
        use_mad = n_usable >= _MIN_MAD_DELTAS and mad >= _MAD_EPSILON

        print(
            f"{colony_id:<14} {n_usable:>8} {median_delta:>12.4f} {mad:>8.4f} {str(use_mad):>7}"
        )

        steps = []
        if use_mad:
            for i in range(1, len(readings)):
                z = _robust_z_for_step(readings, i, median_delta, mad)
                if z is None:
                    continue
                prev = readings[i - 1]
                curr = readings[i]
                elapsed_h = (curr.observed_at - prev.observed_at).total_seconds() / 3600
                delta_kg = curr.weight_kg - prev.weight_kg
                steps.append((curr.observed_at, delta_kg, elapsed_h, z))
        all_steps[colony_id] = steps

    # --- Steps with |z| >= 3 per colony ---
    print("\n\nSteps with |robust_z| >= 3.0\n")
    any_found = False
    for colony_id in sorted(all_steps):
        big = [(ts, dk, eh, z) for ts, dk, eh, z in all_steps[colony_id] if abs(z) >= 3.0]
        if not big:
            continue
        any_found = True
        print(f"  {colony_id}")
        for ts, delta_kg, elapsed_h, z in sorted(big, key=lambda x: abs(x[3]), reverse=True):
            print(f"    {ts.strftime('%Y-%m-%d %H:%M UTC')}  Δ={delta_kg:+.3f} kg  "
                  f"elapsed={elapsed_h:.1f}h  z={z:+.2f}")
    if not any_found:
        print("  (none)")

    # --- Explicit callout: 6LR harvest ---
    print("\n\n=== 6LR 2026-07-07 21:00 UTC harvest callout ===\n")
    for side in ("L", "R"):
        colony_id = f"6LR:{side}"
        readings = sorted(by_colony.get(colony_id, []), key=lambda r: r.timestamp)
        if not readings:
            print(f"  {colony_id}: no readings in window")
            continue
        median_delta, mad, n_usable = _robust_step_stats(readings)
        use_mad = n_usable >= _MIN_MAD_DELTAS and mad >= _MAD_EPSILON

        found = False
        for i in range(1, len(readings)):
            curr = readings[i]
            prev = readings[i - 1]
            # Match by UTC hour
            curr_utc = curr.observed_at.astimezone(UTC)
            if curr_utc.replace(tzinfo=None) == HARVEST_TS.replace(tzinfo=None):
                elapsed_h = (curr.observed_at - prev.observed_at).total_seconds() / 3600
                delta_kg = curr.weight_kg - prev.weight_kg
                pct = delta_kg / prev.weight_kg * 100
                if use_mad:
                    z = _robust_z_for_step(readings, i, median_delta, mad)
                    z_str = f"{z:+.2f}" if z is not None else "N/A"
                else:
                    z_str = "N/A (MAD unavailable)"
                print(f"  {colony_id}: {prev.weight_kg:.3f} → {curr.weight_kg:.3f} kg  "
                      f"Δ={delta_kg:+.3f} kg ({pct:+.1f}%)  "
                      f"elapsed={elapsed_h:.1f}h  z={z_str}")
                found = True
                break
        if not found:
            print(f"  {colony_id}: no reading found at {HARVEST_TS} (may be outside window)")

    # --- Max z from ordinary (non-event) steps per colony ---
    print("\n\nMax |robust_z| among non-event steps (ordinary foraging upper bound)\n")
    for colony_id in sorted(by_colony):
        readings = sorted(by_colony[colony_id], key=lambda r: r.timestamp)
        confirmed = {e.observed_at for e in detect_weight_events(readings)}
        median_delta, mad, n_usable = _robust_step_stats(readings)
        use_mad = n_usable >= _MIN_MAD_DELTAS and mad >= _MAD_EPSILON
        if not use_mad:
            print(f"  {colony_id:<14}  MAD unavailable (n={n_usable})")
            continue

        max_z = 0.0
        max_ts = None
        for i in range(1, len(readings)):
            curr = readings[i]
            if curr.observed_at in confirmed:
                continue
            z = _robust_z_for_step(readings, i, median_delta, mad)
            if z is not None and abs(z) > max_z:
                max_z = abs(z)
                max_ts = curr.observed_at
        ts_str = max_ts.strftime("%Y-%m-%d %H:%M UTC") if max_ts else "—"
        print(f"  {colony_id:<14}  max |z| = {max_z:.2f}  at {ts_str}")


if __name__ == "__main__":
    main()
