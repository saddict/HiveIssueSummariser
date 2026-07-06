from __future__ import annotations

import statistics

from .models import SensorReading

# Reference thermal differential (°C) from Kovac & Stabentheiner, Royal Society Interface, 2026.
# Represents the maximum expected metabolic temperature lift in a dead or unoccupied hive.
T_D_C = 34.5

MIN_THERMAL_POINTS = 10


def thermal_efficiency(readings: list[SensorReading]) -> dict | None:
    """OLS regression T_H_C = m·T_E_C + delta_T over paired internal/external readings.

    Returns {"m", "delta_t", "Pi", "r_squared", "n"} or None if there are too few
    paired readings or if T_E is flat (no variance to regress against).

    m        — weather-tracking coefficient: 0 = perfect insulator, 1 = no thermoregulation
    delta_t  — metabolic temperature lift (°C)
    Pi       — thermal efficiency: delta_t / T_d; higher is healthier
    """
    pairs = [
        (_f_to_c(r.internal_temp_f), _f_to_c(r.external_temp_f))
        for r in readings
        if r.external_temp_f is not None
    ]
    n = len(pairs)
    if n < MIN_THERMAL_POINTS:
        return None

    x_vals = [p[1] for p in pairs]  # T_E_C
    y_vals = [p[0] for p in pairs]  # T_H_C

    x_mean = statistics.fmean(x_vals)
    y_mean = statistics.fmean(y_vals)

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)

    if abs(denominator) < 1e-9:
        return None

    m = numerator / denominator
    delta_t = y_mean - m * x_mean
    pi = delta_t / T_D_C

    y_hat = [m * x + delta_t for x in x_vals]
    ss_res = sum((y - yh) ** 2 for y, yh in zip(y_vals, y_hat))
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    r_squared = 0.0 if abs(ss_tot) < 1e-9 else 1.0 - ss_res / ss_tot

    return {"m": m, "delta_t": delta_t, "Pi": pi, "r_squared": r_squared, "n": n}


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9
