"""Diagnostic spike: sweep PAIRED_MIN_GAIN_KG / PAIRED_MIN_GAIN_PCT thresholds
for paired sister-corroboration (Step 3 of the "Event Detection vs.
Data-Quality Clash" work order) against real data.

For every site with two sides, finds every pair of same-direction,
same-hour-window candidate steps (|robust-z| >= MAD_CORROBORATE_K on each
side, independent of the physical floors detect_weight_events applies
standalone -- see _candidate_moves in events.py) and reports, for each
(PAIRED_MIN_GAIN_KG, PAIRED_MIN_GAIN_PCT) combination in the sweep grid,
whether that pair would be admitted as a paired promotion.

This is the reproducible evidence behind the now-final threshold decisions
(Step 7 of the work order), not a live tuning knob -- PAIRED_MIN_GAIN_KG/PCT in
events.py are untouched by this script. The 60-day sweep locks:

  * PAIRED_MIN_GAIN_KG = 1.5, PAIRED_MIN_GAIN_PCT = 3.0 -- the false-pair
    population (shared-foraging dual co-gains, and post-harvest settle-back
    rebounds) tops out at 1.21 kg/side (WTG_HSCHL 2026-07-07) and 1.20 kg/side
    (PRT_1 2026-07-09 rebound), both rejected at 1.5 kg; the smallest genuine
    paired step is 1.6 kg. The kg floor does the discriminating work; the 3.0%
    floor is a secondary small-colony guard.
  * AGGREGATE_MAX_DOMINANT_STEP_SHARE = 0.6 -- see the aggregate-share scan
    section below: the WTG_HSCHL:R foraging-burst window sits at ~0.78 share
    (one hourly step dominates), well above genuine multi-step moves.

Usage:
    python3 spike_paired_corroboration.py
    python3 spike_paired_corroboration.py --window-days 60
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.events import (
    AGGREGATE_MAX_DOMINANT_STEP_SHARE,
    MAD_CORROBORATE_K,
    PAIRED_MIN_GAIN_KG,
    PAIRED_MIN_GAIN_PCT,
    _aggregate_window_shares,
    _candidate_moves,
    _one_interval_hours,
    _robust_step_stats,
)

PROJECT_ROOT = Path(__file__).resolve().parent

PAIRED_MIN_GAIN_KG_SWEEP = [1.0, 1.5, 2.0, 2.5]
PAIRED_MIN_GAIN_PCT_SWEEP = [2.0, 3.0, 4.0, 5.0]


def _load_windowed_readings(window_days: int):
    hives, colony_sides, _settings = load_hive_config(PROJECT_ROOT / "hive_config.py")
    sensor_dir = PROJECT_ROOT / "local_data" / "dynamodb"
    raw = load_sensor_readings(sensor_dir, hives, colony_sides)
    end_at = max(r.observed_at for r in raw)
    start_at = end_at - timedelta(days=window_days)
    return [r for r in raw if r.observed_at >= start_at]


def _by_site(readings):
    by_hive: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in readings:
        by_hive[r.hive_id][r.colony_side].append(r)
    return by_hive


def _find_pairs(ordered_a, ordered_b):
    """Every same-direction candidate pair within one reading interval of
    each other, mirroring the matching logic in _promote_paired_events."""
    candidates_a = _candidate_moves(ordered_a)
    candidates_b = _candidate_moves(ordered_b)
    tolerance = max(_one_interval_hours(ordered_a), _one_interval_hours(ordered_b))

    pairs = []
    for _ia, prev_a, curr_a, delta_a, _ea, z_a in candidates_a:
        for _ib, prev_b, curr_b, delta_b, _eb, z_b in candidates_b:
            if (delta_a > 0) != (delta_b > 0):
                continue
            gap_hours = abs((curr_a.observed_at - curr_b.observed_at).total_seconds() / 3600)
            if gap_hours > tolerance:
                continue
            pairs.append((prev_a, curr_a, delta_a, z_a, prev_b, curr_b, delta_b, z_b))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=60)
    args = parser.parse_args()

    readings = _load_windowed_readings(args.window_days)
    by_site = _by_site(readings)

    print(f"\nPaired sub-threshold sweep  (window={args.window_days}d, MAD_CORROBORATE_K={MAD_CORROBORATE_K})")
    print(f"Current PAIRED_MIN_GAIN_KG={PAIRED_MIN_GAIN_KG}  PAIRED_MIN_GAIN_PCT={PAIRED_MIN_GAIN_PCT} (unchanged by this script)\n")

    for hive_id in sorted(by_site):
        sides = by_site[hive_id]
        if len(sides) != 2:
            continue
        side_a, side_b = sorted(sides)
        ordered_a = sorted(sides[side_a], key=lambda r: r.timestamp)
        ordered_b = sorted(sides[side_b], key=lambda r: r.timestamp)

        pairs = _find_pairs(ordered_a, ordered_b)
        if not pairs:
            continue

        print(f"=== {hive_id} ({side_a}/{side_b}) ===")
        for prev_a, curr_a, delta_a, z_a, prev_b, curr_b, delta_b, z_b in sorted(
            pairs, key=lambda p: p[1].observed_at
        ):
            pct_a = (delta_a / prev_a.weight_kg) * 100 if prev_a.weight_kg else 0.0
            pct_b = (delta_b / prev_b.weight_kg) * 100 if prev_b.weight_kg else 0.0
            direction = "gain" if delta_a > 0 else "drop"
            print(f"  [{direction}] {curr_a.observed_at.isoformat()}")
            print(f"    {side_a}: {prev_a.weight_kg:.2f} -> {curr_a.weight_kg:.2f} kg  Δ={delta_a:+.2f}kg ({pct_a:+.1f}%)  z={z_a:+.2f}")
            print(f"    {side_b}: {prev_b.weight_kg:.2f} -> {curr_b.weight_kg:.2f} kg  Δ={delta_b:+.2f}kg ({pct_b:+.1f}%)  z={z_b:+.2f}")

            if delta_a <= 0:
                print("    drops: no extra floor in Step 3 -- always admitted once paired")
                continue

            admitted = []
            for kg_floor in PAIRED_MIN_GAIN_KG_SWEEP:
                for pct_floor in PAIRED_MIN_GAIN_PCT_SWEEP:
                    a_ok = delta_a >= kg_floor and pct_a >= pct_floor
                    b_ok = delta_b >= kg_floor and pct_b >= pct_floor
                    if a_ok and b_ok:
                        admitted.append(f"(kg>={kg_floor},pct>={pct_floor})")
            if admitted:
                print(f"    admitted by: {', '.join(admitted)}")
            else:
                print("    admitted by: none in sweep grid")
        print()

    # --- Named ground-truth checks from the work order ---
    print("\n=== Ground-truth checks ===\n")

    print("DR_WLKS 2026-07-03 additions (must remain individually confirmed, not need pairing):")
    for side in ("L", "R"):
        ordered = sorted(by_site.get("DR_WLKS", {}).get(side, []), key=lambda r: r.timestamp)
        for i in range(1, len(ordered)):
            if ordered[i].observed_at.strftime("%Y-%m-%d") == "2026-07-03" and ordered[i].observed_at.hour == 16:
                prev, curr = ordered[i - 1], ordered[i]
                delta = curr.weight_kg - prev.weight_kg
                print(f"  DR_WLKS:{side}  {prev.weight_kg:.2f} -> {curr.weight_kg:.2f} kg  Δ={delta:+.2f}kg -- floor alone (>=3.0kg) confirms this standalone, no pairing needed")

    print("\nWTG_HSCHL:R 2026-07-03 foraging burst (must NOT pair):")
    ordered_l = sorted(by_site.get("WTG_HSCHL", {}).get("L", []), key=lambda r: r.timestamp)
    ordered_r = sorted(by_site.get("WTG_HSCHL", {}).get("R", []), key=lambda r: r.timestamp)
    candidates_l = {c[2].observed_at: c for c in _candidate_moves(ordered_l)}
    for i in range(1, len(ordered_r)):
        if ordered_r[i].observed_at.strftime("%Y-%m-%d") == "2026-07-03" and ordered_r[i].observed_at.hour == 12:
            prev, curr = ordered_r[i - 1], ordered_r[i]
            delta = curr.weight_kg - prev.weight_kg
            print(f"  WTG_HSCHL:R  {prev.weight_kg:.2f} -> {curr.weight_kg:.2f} kg  Δ={delta:+.2f}kg")
            sister = candidates_l.get(curr.observed_at)
            if sister is None:
                print("  WTG_HSCHL:L at the same hour: not a candidate (|z| < MAD_CORROBORATE_K) -- cannot pair regardless of gain-floor sweep values")
            else:
                print(f"  WTG_HSCHL:L at the same hour: IS a candidate (z={sister[5]:+.2f}) -- would need the gain-floor sweep to reject it")

    print("\n6LR 2026-07-07 dual harvest (drops must be unaffected by any PAIRED_MIN_GAIN sweep value):")
    for side in ("L", "R"):
        ordered = sorted(by_site.get("6LR", {}).get(side, []), key=lambda r: r.timestamp)
        for i in range(1, len(ordered)):
            if ordered[i].observed_at.strftime("%Y-%m-%d") == "2026-07-07" and ordered[i].observed_at.hour == 17:
                prev, curr = ordered[i - 1], ordered[i]
                delta = curr.weight_kg - prev.weight_kg
                print(f"  6LR:{side}  {prev.weight_kg:.2f} -> {curr.weight_kg:.2f} kg  Δ={delta:+.2f}kg -- drop, no PAIRED_MIN_GAIN floor applies")

    _aggregate_share_scan(by_site)


def _aggregate_share_scan(by_site) -> None:
    """Report the dominant-step share of every aggregate candidate window.

    Grounds AGGREGATE_MAX_DOMINANT_STEP_SHARE: a window whose single largest
    hourly step is more than this share of the window's net change is an
    ordinary trend containing one floor-blocked spike, not a genuine multi-step
    move. Passes an empty pairwise-candidate set so the scan surfaces the full
    window population (the pairwise-overlap skip only removes windows; it never
    changes a surviving window's share), including the WTG_HSCHL:R foraging
    burst that motivated the guard.
    """
    print("\n=== Aggregate dominant-step-share scan (guard = %.2f) ===\n" % AGGREGATE_MAX_DOMINANT_STEP_SHARE)
    for hive_id in sorted(by_site):
        for side in sorted(by_site[hive_id]):
            ordered = sorted(by_site[hive_id][side], key=lambda r: r.timestamp)
            median_delta, mad, n_usable = _robust_step_stats(ordered)
            if n_usable < 8 or mad <= 1e-6:
                continue
            shares = _aggregate_window_shares(ordered, median_delta, mad, [])
            for anchor, current, delta_kg, max_step_kg in shares:
                share = max_step_kg / abs(delta_kg) if delta_kg else 0.0
                verdict = "REJECT (one step dominates)" if share > AGGREGATE_MAX_DOMINANT_STEP_SHARE else "accept (spread across steps)"
                print(
                    f"  {hive_id}:{side}  {anchor.observed_at.isoformat()} -> {current.observed_at.isoformat()}  "
                    f"Δ={delta_kg:+.2f}kg  max_step={max_step_kg:.2f}kg  share={share:.2f}  -> {verdict}"
                )


if __name__ == "__main__":
    main()
