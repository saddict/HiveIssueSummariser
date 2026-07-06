from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from beemon_scoring.models import SensorReading
from beemon_scoring.thermal import MIN_THERMAL_POINTS, T_D_C, thermal_efficiency

TZ = ZoneInfo("America/New_York")


def _reading(internal_f: float, external_f: float | None, ts: int = 0) -> SensorReading:
    return SensorReading(
        hive_id="test",
        region_id="region_a",
        colony_side="L",
        device_uid="device",
        timestamp=ts,
        observed_at=datetime(2026, 6, 1, 12, 0, tzinfo=TZ),
        weight_kg=40.0,
        internal_temp_f=internal_f,
        internal_humidity_pct=55.0,
        external_temp_f=external_f,
        external_humidity_pct=60.0,
    )


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _readings_from_celsius_pairs(pairs: list[tuple[float, float]]) -> list[SensorReading]:
    return [_reading(_c_to_f(t_h), _c_to_f(t_e), ts=i) for i, (t_h, t_e) in enumerate(pairs)]


class TestThermalEfficiency(unittest.TestCase):

    def test_weather_tracking_hive_high_m_zero_pi(self):
        # T_H = T_E exactly → slope = 1, intercept = 0, Pi = 0
        pairs = [(t, t) for t in range(10, 30)]  # 20 points
        readings = _readings_from_celsius_pairs(pairs)
        result = thermal_efficiency(readings)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["m"], 1.0, places=6)
        self.assertAlmostEqual(result["delta_t"], 0.0, places=6)
        self.assertAlmostEqual(result["Pi"], 0.0, places=6)
        self.assertAlmostEqual(result["r_squared"], 1.0, places=6)
        self.assertEqual(result["n"], 20)

    def test_mid_case_exact_fit(self):
        # T_H_C = 0.5 * T_E_C + 17.25 → m=0.5, delta_t=17.25, Pi=17.25/34.5=0.5
        t_e_vals = list(range(10, 30))
        pairs = [(0.5 * t_e + 17.25, t_e) for t_e in t_e_vals]
        readings = _readings_from_celsius_pairs(pairs)
        result = thermal_efficiency(readings)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["m"], 0.5, places=6)
        self.assertAlmostEqual(result["delta_t"], 17.25, places=6)
        self.assertAlmostEqual(result["Pi"], 0.5, places=6)
        self.assertAlmostEqual(result["r_squared"], 1.0, places=6)

    def test_delta_t_recovery_arithmetic(self):
        # Pi = delta_t / T_d: verify the formula holds for an arbitrary fit
        t_e_vals = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0]
        delta_t_expected = 20.7
        m_expected = 0.3
        pairs = [(m_expected * t_e + delta_t_expected, t_e) for t_e in t_e_vals]
        readings = _readings_from_celsius_pairs(pairs)
        result = thermal_efficiency(readings)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["delta_t"], delta_t_expected, places=6)
        self.assertAlmostEqual(result["Pi"], delta_t_expected / T_D_C, places=6)

    def test_stable_hive_near_zero_m(self):
        # T_H ≈ constant (35°C) regardless of varying T_E → m ≈ 0, delta_t ≈ 35
        t_e_vals = list(range(5, 35))  # 30 points spanning 30°C
        pairs = [(35.0, t_e) for t_e in t_e_vals]
        readings = _readings_from_celsius_pairs(pairs)
        result = thermal_efficiency(readings)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["m"], 0.0, places=6)
        self.assertAlmostEqual(result["delta_t"], 35.0, places=6)
        self.assertAlmostEqual(result["Pi"], 35.0 / T_D_C, places=5)

    def test_degenerate_too_few_points(self):
        # n < MIN_THERMAL_POINTS → return None
        pairs = [(35.0, t) for t in range(MIN_THERMAL_POINTS - 1)]
        readings = _readings_from_celsius_pairs(pairs)
        self.assertIsNone(thermal_efficiency(readings))

    def test_exactly_min_points_is_accepted(self):
        pairs = [(0.4 * t + 20.0, t) for t in range(MIN_THERMAL_POINTS)]
        readings = _readings_from_celsius_pairs(pairs)
        result = thermal_efficiency(readings)
        self.assertIsNotNone(result)
        self.assertEqual(result["n"], MIN_THERMAL_POINTS)

    def test_flat_external_temp_returns_none(self):
        # All T_E identical → SS_x = 0 → no fit possible
        pairs = [(35.0, 20.0)] * 15  # 15 points, all T_E = 20°C
        readings = _readings_from_celsius_pairs(pairs)
        self.assertIsNone(thermal_efficiency(readings))

    def test_missing_external_temp_excluded_from_n(self):
        # Readings with external_temp_f=None should not count toward n
        good_pairs = [(0.5 * t + 17.25, t) for t in range(10, 20)]  # 10 valid
        good_readings = _readings_from_celsius_pairs(good_pairs)
        null_readings = [_reading(94.0, None, ts=100 + i) for i in range(20)]
        result = thermal_efficiency(good_readings + null_readings)
        self.assertIsNotNone(result)
        self.assertEqual(result["n"], 10)

    def test_all_external_temp_missing_returns_none(self):
        readings = [_reading(94.0, None, ts=i) for i in range(20)]
        self.assertIsNone(thermal_efficiency(readings))


if __name__ == "__main__":
    unittest.main()
