from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from beemon_scoring.models import ColonyFeatures, SensorReading
from beemon_scoring.scoring import _daily_weight_pct_changes, _eligible_metric_peers


TZ = ZoneInfo("America/New_York")


def reading(hive_id: str, side: str, timestamp: int, weight: float, observed_at: datetime) -> SensorReading:
    return SensorReading(
        hive_id=hive_id,
        colony_side=side,
        device_uid="device",
        timestamp=timestamp,
        observed_at=observed_at,
        weight_lb=weight,
        internal_temp_f=94.0,
        internal_humidity_pct=55.0,
        external_temp_f=70.0,
        external_humidity_pct=60.0,
    )


def feature(colony_id: str, favorable_windows: int, poor_windows: int) -> ColonyFeatures:
    return ColonyFeatures(
        colony_id=colony_id,
        hive_id=colony_id.split(":")[0],
        colony_side=colony_id.split(":")[1],
        sample_count=10,
        excluded_reading_count=0,
        data_quality_flags=[],
        start_at=datetime(2026, 6, 11, tzinfo=TZ),
        end_at=datetime(2026, 6, 18, tzinfo=TZ),
        days_observed=7.0,
        latest_weight_lb=10.0,
        weight_delta_lb=0.0,
        weight_pct_change=0.0,
        weight_slope_lb_per_day=0.0,
        weight_slope_pct_per_day=0.0,
        favorable_weather_window_count=favorable_windows,
        poor_weather_window_count=poor_windows,
        favorable_weather_weight_slope_pct_per_day=0.0,
        poor_weather_weight_loss_pct=0.0,
        avg_internal_temp_f=94.0,
        internal_temp_std_f=0.0,
        avg_brood_temp_deviation_f=0.5,
        avg_internal_humidity_pct=55.0,
        internal_humidity_std_pct=0.0,
        high_humidity_reading_pct=0.0,
        low_humidity_reading_pct=0.0,
        avg_external_temp_f=70.0,
        avg_external_humidity_pct=60.0,
        avg_weather_temp_f=70.0,
        avg_weather_humidity_pct=60.0,
        rainy_weather_reading_pct=0.0,
        cloudy_weather_reading_pct=0.0,
        dominant_weather_overview=None,
    )


class ScoringLogicTests(unittest.TestCase):
    def test_daily_weight_pct_changes_do_not_span_weather_gaps(self) -> None:
        readings = [
            reading("A", "L", 1, 100.0, datetime(2026, 6, 11, 7, tzinfo=TZ)),
            reading("A", "L", 2, 101.0, datetime(2026, 6, 11, 19, tzinfo=TZ)),
            reading("A", "L", 3, 80.0, datetime(2026, 6, 12, 7, tzinfo=TZ)),
            reading("A", "L", 4, 90.0, datetime(2026, 6, 12, 19, tzinfo=TZ)),
            reading("A", "L", 5, 101.0, datetime(2026, 6, 13, 7, tzinfo=TZ)),
            reading("A", "L", 6, 100.0, datetime(2026, 6, 13, 19, tzinfo=TZ)),
        ]
        day_types = {
            date(2026, 6, 11): "favorable",
            date(2026, 6, 12): "neutral",
            date(2026, 6, 13): "favorable",
        }

        changes = _daily_weight_pct_changes(readings, day_types, "favorable")

        self.assertEqual(len(changes), 2)
        self.assertAlmostEqual(changes[0], 1.0)
        self.assertAlmostEqual(changes[1], -0.990099, places=5)

    def test_weather_metric_peer_eligibility_requires_matching_windows(self) -> None:
        features = [
            feature("A:L", favorable_windows=1, poor_windows=0),
            feature("B:L", favorable_windows=0, poor_windows=1),
            feature("C:L", favorable_windows=2, poor_windows=1),
        ]
        metric = {"min_sample_attr": "favorable_weather_window_count", "min_sample_count": 1}

        eligible = _eligible_metric_peers(features, metric)

        self.assertEqual([item.colony_id for item in eligible], ["A:L", "C:L"])


if __name__ == "__main__":
    unittest.main()
