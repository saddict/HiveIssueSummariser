#!/usr/bin/env python3
"""Thermal efficiency validation spike.

Fits T_H_C = m·T_E_C + ΔT for each colony and prints a diagnostic table.
Does NOT modify the scoring engine. Run to validate the model before integration.

Usage:
    python3 spike_thermal_efficiency.py
    python3 spike_thermal_efficiency.py --window-days 30
"""
from __future__ import annotations

import argparse
import statistics
from datetime import timedelta
from pathlib import Path

from beemon_scoring.data_loader import load_hive_config, load_sensor_readings
from beemon_scoring.models import SensorReading
from beemon_scoring.quality import filter_quality_issues

T_D_C = 34.5  # Reference thermal differential (°C) — Kovac & Stabentheiner, RSI 2026


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def _ols_thermal(readings: list[SensorReading]) -> dict | None:
    """Fit T_H_C = m·T_E_C + ΔT via OLS. Returns diagnostics or None."""
    pairs = [
        (_f_to_c(r.internal_temp_f), _f_to_c(r.external_temp_f))
        for r in readings
        if r.external_temp_f is not None
    ]
    n = len(pairs)
    if n < 3:
        return None

    x_vals = [p[1] for p in pairs]  # T_E_C
    y_vals = [p[0] for p in pairs]  # T_H_C

    x_mean = statistics.fmean(x_vals)
    y_mean = statistics.fmean(y_vals)

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    if abs(denominator) < 1e-9:
        return None  # Flat T_E — can't fit

    m = numerator / denominator
    delta_t = y_mean - m * x_mean
    pi = delta_t / T_D_C

    y_hat = [m * x + delta_t for x in x_vals]
    ss_res = sum((y - yh) ** 2 for y, yh in zip(y_vals, y_hat))
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    r2 = 0.0 if abs(ss_tot) < 1e-9 else 1.0 - ss_res / ss_tot

    return {"m": m, "delta_t": delta_t, "Pi": pi, "r2": r2, "n": n}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=7)
    args = parser.parse_args()

    root = Path(__file__).parent
    hives, colony_sides, settings = load_hive_config(root / "hive_config.py")
    sensor_dir = root / "local_data" / "dynamodb"

    all_readings = load_sensor_readings(sensor_dir, hives, colony_sides)
    if not all_readings:
        print("No sensor readings found.")
        return

    end_at = max(r.observed_at for r in all_readings)
    start_at = end_at - timedelta(days=args.window_days)
    windowed = [r for r in all_readings if r.observed_at >= start_at]
    filtered, _, _ = filter_quality_issues(windowed)

    by_colony: dict[str, list[SensorReading]] = {}
    for reading in filtered:
        by_colony.setdefault(reading.colony_id, []).append(reading)

    print(f"\nThermal efficiency spike — {args.window_days}-day window")
    print(f"Window: {start_at.date()} → {end_at.date()}")
    print()
    print(f"{'Colony':<18} {'n':>5}  {'m':>6}  {'ΔT°C':>7}  {'Pi':>6}  {'R²':>6}  {'status'}")
    print("-" * 65)

    for colony_id in sorted(by_colony):
        readings = by_colony[colony_id]
        result = _ols_thermal(readings)
        if result is None:
            n_pairs = sum(1 for r in readings if r.external_temp_f is not None)
            print(f"{colony_id:<18} {n_pairs:>5}  {'—':>6}  {'—':>7}  {'—':>6}  {'—':>6}  INSUFFICIENT DATA")
            continue

        m, dt, pi, r2, n = result["m"], result["delta_t"], result["Pi"], result["r2"], result["n"]
        ok_r2 = r2 >= 0.3
        ok_m = 0.0 < m < 1.0
        ok_n = n >= 10
        verdict = "OK" if (ok_r2 and ok_m and ok_n) else f"WARN({'R²' if not ok_r2 else ''}{'m' if not ok_m else ''}{'n' if not ok_n else ''})"
        print(f"{colony_id:<18} {n:>5}  {m:>6.3f}  {dt:>7.2f}  {pi:>6.3f}  {r2:>6.3f}  {verdict}")

    print()
    print("Go criteria: R²>0.3, m∈(0,1), n≥10 for all colonies")


if __name__ == "__main__":
    main()
