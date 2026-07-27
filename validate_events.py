"""Validate weight-event detection against the beekeeper ground-truth log.

Runs detection over the cached history exactly as production does (single source
of truth: detect_site_events on the *pre-filter* readings, matching build_scores
in scoring.py) and reports precision / recall / F1 and a confusion matrix against
ground_truth/events.json.

Usage:
    python3 validate_events.py
    python3 validate_events.py --tolerance-hours 6
    python3 validate_events.py --windowed 7          # rolling-window operational recall
    python3 validate_events.py --json output/event_validation.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.events import detect_site_events
from beemon_scoring.validation import base_kind, load_ground_truth, match_events

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_raw_readings():
    hives, colony_sides, _ = load_hive_config(PROJECT_ROOT / "hive_config.py")
    return load_sensor_readings(PROJECT_ROOT / "local_data" / "dynamodb", hives, colony_sides)


def _detect_full_history(readings) -> dict[str, list]:
    return detect_site_events(readings)


def _detect_windowed(readings, window_days: int, step_days: int = 1) -> dict[str, list]:
    """Replay production N-day rolling windows and union the detected events
    (dedup by colony + timestamp) — the operational-recall view."""
    if not readings:
        return {}
    end = max(r.observed_at for r in readings)
    start = min(r.observed_at for r in readings)
    seen: dict[str, set] = defaultdict(set)
    unioned: dict[str, list] = defaultdict(list)
    window = timedelta(days=window_days)
    step = timedelta(days=step_days)
    cursor = start + window
    while cursor <= end + step:
        lo = cursor - window
        windowed = [r for r in readings if lo <= r.observed_at <= cursor]
        for colony_id, events in detect_site_events(windowed).items():
            for ev in events:
                key = ev.observed_at
                if key not in seen[colony_id]:
                    seen[colony_id].add(key)
                    unioned[colony_id].append(ev)
        cursor += step
    return dict(unioned)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ground-truth", type=Path, default=PROJECT_ROOT / "ground_truth" / "events.json")
    parser.add_argument("--tolerance-hours", type=float, default=6.0)
    parser.add_argument("--windowed", type=int, default=None, metavar="N",
                        help="Replay N-day rolling windows instead of full-history detection.")
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    readings = _load_raw_readings()
    if not readings:
        print("No sensor readings found.")
        return

    if args.windowed:
        detected = _detect_windowed(readings, args.windowed, args.step_days)
        mode = f"rolling {args.windowed}-day windows (step {args.step_days}d)"
    else:
        detected = _detect_full_history(readings)
        mode = "full history"

    records, audited = load_ground_truth(args.ground_truth)
    result = match_events(detected, records, audited, tolerance_hours=args.tolerance_hours)

    total_detected = sum(len(evts) for evts in detected.values())
    print(f"\nEvent detection validation  ({mode}, tolerance {args.tolerance_hours}h)")
    print(f"Ground truth: {len([r for r in records if r.label=='event'])} events, "
          f"{len([r for r in records if r.label=='non_event'])} non-events, "
          f"{len(audited)} audited ranges.  Detector produced {total_detected} events.\n")

    print(f"  Precision: {result.precision:.3f}")
    print(f"  Recall:    {result.recall:.3f}")
    print(f"  F1:        {result.f1:.3f}")
    print(f"  Confusion: {result.confusion()}")
    offsets = result.tp_offset_hours()
    if offsets:
        print(f"  TP time offsets (h): min={min(offsets):+.1f} max={max(offsets):+.1f} "
              f"mean={sum(offsets)/len(offsets):+.2f}")

    # Per base-kind precision/recall.
    kinds = sorted({base_kind(k) for r in records for k in r.kinds})
    print("\n  By kind:")
    for kind in kinds:
        tp = sum(1 for e in result.true_positives if kind in e["kinds"])
        fn = sum(1 for e in result.false_negatives if kind in e["kinds"])
        fp = sum(1 for e in result.false_positives if kind in e.get("kinds", []))
        rec = tp / (tp + fn) if (tp + fn) else 1.0
        print(f"    {kind:<10} TP={tp} FN={fn} FP={fp}  recall={rec:.3f}")

    if result.false_negatives:
        print("\n  Missed (false negatives):")
        for e in result.false_negatives:
            print(f"    {e['colony_id']} {e['observed_at']} {e['kinds']} — {e['notes']}")
    if result.false_positives:
        print("\n  False alarms (false positives):")
        for e in result.false_positives:
            print(f"    {e['colony_id']} {e.get('detected_at')} {e.get('detected_kind')} — {e.get('reason')}")
    if result.unaudited_detections:
        print(f"\n  {len(result.unaudited_detections)} detections outside audited ranges "
              f"(excluded from precision — label them to extend the ground truth).")

    if args.json:
        payload = {
            "mode": mode,
            "tolerance_hours": args.tolerance_hours,
            "precision": round(result.precision, 4),
            "recall": round(result.recall, 4),
            "f1": round(result.f1, 4),
            "confusion": result.confusion(),
            "true_positives": result.true_positives,
            "false_negatives": result.false_negatives,
            "false_positives": result.false_positives,
            "unaudited_detections": result.unaudited_detections,
        }
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
