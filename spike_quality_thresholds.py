"""Diagnostic spike: empirically ground the data-quality jump thresholds.

The jump thresholds in beemon_scoring/quality.py (MAX_WEIGHT_JUMP_KG=3.63 &
MAX_WEIGHT_JUMP_PCT=12.0 jointly, MAX_TEMP_JUMP_F=25.0, MAX_HUMIDITY_JUMP_PCT=45.0
over MAX_JUMP_INTERVAL_HOURS=6.0) are heuristics. This script characterises the
real inter-reading movement distribution per sensor channel so each threshold can
be justified against an observed percentile rather than a guess.

Method:
  * Load the full RAW history (thresholds gate raw data, so characterise
    pre-filter movement).
  * Detect events once with detect_site_events and exclude every consecutive pair
    whose later reading falls within EVENT_SETTLE_WINDOW_HOURS of an event -- the
    same exemption filter_quality_issues applies -- so the "normal movement"
    distribution is not polluted by genuine harvests/supering. Those excluded
    event-adjacent deltas are reported separately as the "event signal".
  * For every remaining pair with 0 < dt <= MAX_JUMP_INTERVAL_HOURS, collect the
    absolute movement in each channel and report percentiles.

Decision rule (documented in quality.py after this runs): keep a threshold if it
sits >= p99.9 of normal movement AND <= the smallest confirmed-event magnitude
(so it excludes only true outliers and never a real event); only if a threshold
lands inside the normal bulk should it be nudged to the observed gap midpoint.

Usage:
    python3 spike_quality_thresholds.py
"""
from __future__ import annotations

from pathlib import Path

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.events import detect_site_events
from beemon_scoring.quality import (
    EVENT_SETTLE_WINDOW_HOURS,
    MAX_HUMIDITY_JUMP_PCT,
    MAX_JUMP_INTERVAL_HOURS,
    MAX_TEMP_JUMP_F,
    MAX_WEIGHT_JUMP_KG,
    MAX_WEIGHT_JUMP_PCT,
    MAX_WEIGHT_KG,
    MIN_WEIGHT_KG,
    MIN_INTERNAL_TEMP_F,
    MAX_INTERNAL_TEMP_F,
)

PROJECT_ROOT = Path(__file__).resolve().parent


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_values):
        return sorted_values[-1]
    return sorted_values[lo] + frac * (sorted_values[lo + 1] - sorted_values[lo])


def _report_channel(name: str, unit: str, normal: list[float], threshold: float,
                    smallest_event: float | None = None) -> None:
    normal = sorted(normal)
    print(f"\n{name} (threshold = {threshold:g} {unit}, over <= {MAX_JUMP_INTERVAL_HOURS:g}h):")
    if not normal:
        print("  no normal-movement pairs")
        return
    for q, label in [(0.50, "p50"), (0.90, "p90"), (0.99, "p99"), (0.999, "p99.9"), (1.0, "max")]:
        print(f"    {label:>6}: {_percentile(normal, q):8.3f} {unit}")
    # Where does the threshold sit within the normal distribution?
    below = sum(1 for v in normal if v <= threshold)
    pctile = 100 * below / len(normal)
    print(f"    threshold {threshold:g} {unit} is at the {pctile:.2f}th percentile of normal movement")
    if smallest_event is not None:
        # Informational only: confirmed events are detected first and are exempt
        # from the jump filter, so no threshold can clip one regardless of size
        # (the smallest events here are deliberately tiny sister-corroborated
        # steps). The threshold's real job is to sit above the normal bulk.
        print(f"    (context) smallest confirmed event magnitude: {smallest_event:.3f} {unit}; "
              f"events bypass this filter, so this is not a clipping risk")
    # Exclusion-count sensitivity.
    for factor in (0.75, 1.0, 1.25):
        excl = sum(1 for v in normal if v > threshold * factor)
        print(f"    at x{factor}: {excl} of {len(normal)} normal pairs excluded")


def main() -> None:
    hives, colony_sides, _ = load_hive_config(PROJECT_ROOT / "hive_config.py")
    raw = load_sensor_readings(PROJECT_ROOT / "local_data" / "dynamodb", hives, colony_sides)
    if not raw:
        print("No sensor readings found.")
        return

    events_by_colony = detect_site_events(raw)

    by_colony: dict[str, list] = {}
    for r in raw:
        by_colony.setdefault(r.colony_id, []).append(r)

    weight_kg_normal, weight_pct_normal, temp_normal, hum_normal = [], [], [], []
    event_kg_magnitudes = [abs(e.delta_kg) for evts in events_by_colony.values() for e in evts]
    event_pct_magnitudes = [abs(e.pct_change) for evts in events_by_colony.values() for e in evts]
    excluded_pairs = 0
    min_w = min_t = float("inf")
    max_w = max_t = float("-inf")

    for colony_id, readings in by_colony.items():
        ordered = sorted(readings, key=lambda x: x.timestamp)
        event_times = sorted(e.observed_at for e in events_by_colony.get(colony_id, []))
        for i in range(1, len(ordered)):
            prev, curr = ordered[i - 1], ordered[i]
            min_w, max_w = min(min_w, curr.weight_kg), max(max_w, curr.weight_kg)
            min_t, max_t = min(min_t, curr.internal_temp_f), max(max_t, curr.internal_temp_f)
            dt = (curr.observed_at - prev.observed_at).total_seconds() / 3600
            if dt <= 0 or dt > MAX_JUMP_INTERVAL_HOURS:
                continue
            if prev.weight_kg <= 0 or curr.weight_kg <= 0:
                continue
            d_w = abs(curr.weight_kg - prev.weight_kg)
            d_w_pct = d_w / prev.weight_kg * 100
            d_t = abs(curr.internal_temp_f - prev.internal_temp_f)
            d_h = abs(curr.internal_humidity_pct - prev.internal_humidity_pct)
            # Is curr within the settle window after an event? If so it is the
            # event signal, not normal movement.
            near_event = any(
                0 <= (curr.observed_at - et).total_seconds() / 3600 <= EVENT_SETTLE_WINDOW_HOURS
                for et in event_times
            )
            if near_event:
                excluded_pairs += 1
            else:
                weight_kg_normal.append(d_w)
                weight_pct_normal.append(d_w_pct)
                temp_normal.append(d_t)
                hum_normal.append(d_h)

    print("Data-quality threshold grounding (full raw history, event-adjacent pairs excluded)")
    print(f"Normal-movement pairs: {len(weight_kg_normal)}  |  event-adjacent pairs excluded: {excluded_pairs} "
          f"|  confirmed events: {len(event_kg_magnitudes)}")

    smallest_event_kg = min(event_kg_magnitudes) if event_kg_magnitudes else None
    smallest_event_pct = min(event_pct_magnitudes) if event_pct_magnitudes else None
    _report_channel("Weight jump (kg)", "kg", weight_kg_normal, MAX_WEIGHT_JUMP_KG, smallest_event_kg)
    _report_channel("Weight jump (%)", "%", weight_pct_normal, MAX_WEIGHT_JUMP_PCT, smallest_event_pct)
    print("    note: the weight filter requires BOTH kg AND % thresholds to be exceeded jointly.")
    _report_channel("Internal temperature jump", "F", temp_normal, MAX_TEMP_JUMP_F)
    _report_channel("Internal humidity jump", "pp", hum_normal, MAX_HUMIDITY_JUMP_PCT)

    print("\nPhysical bounds (justified as impossible, not statistical):")
    print(f"    weight observed [{min_w:.2f}, {max_w:.2f}] kg  vs bounds [{MIN_WEIGHT_KG}, {MAX_WEIGHT_KG}]")
    print(f"    internal temp observed [{min_t:.1f}, {max_t:.1f}] F  vs bounds [{MIN_INTERNAL_TEMP_F}, {MAX_INTERNAL_TEMP_F}]")


if __name__ == "__main__":
    main()
