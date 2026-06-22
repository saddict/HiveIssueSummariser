from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from beemon_scoring.models import ColonyFeatures, ColonyScore, MetricComparison, SensorReading
from beemon_scoring.scoring import _daily_weight_pct_changes, _eligible_metric_peers
from beemon_scoring.sister_comparison import build_sister_comparisons


TZ = ZoneInfo("America/New_York")


def reading(hive_id: str, side: str, timestamp: int, weight: float, observed_at: datetime) -> SensorReading:
    return SensorReading(
        hive_id=hive_id,
        colony_side=side,
        device_uid="device",
        timestamp=timestamp,
        observed_at=observed_at,
        weight_kg=weight,
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
        latest_weight_kg=10.0,
        weight_delta_kg=0.0,
        weight_pct_change=0.0,
        weight_slope_kg_per_day=0.0,
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


def colony_score(colony_id: str, side: str, weight_pct_change: float, latest_weight_kg: float = 10.0) -> ColonyScore:
    base = feature(colony_id, favorable_windows=1, poor_windows=1)
    base.weight_pct_change = weight_pct_change
    base.latest_weight_kg = latest_weight_kg
    return ColonyScore(
        colony_id=colony_id,
        hive_id=colony_id.split(":")[0],
        colony_side=side,
        score=0.0,
        status="normal",
        comparisons=[
            MetricComparison(
                metric="latest_weight_kg",
                label="current colony weight",
                value=latest_weight_kg,
                peer_mean=30.0,
                peer_std=10.0,
                badness_z=0.0,
                weight=0.30,
                unit="kg",
            ),
            MetricComparison(
                metric="weight_pct_change",
                label="7-day weight percent change",
                value=weight_pct_change,
                peer_mean=0.0,
                peer_std=2.0,
                badness_z=0.0,
                weight=0.17,
                unit="%",
            )
        ],
        feature=base,
        flags=[],
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

    def test_sister_comparison_marks_lower_weight_percent_change_as_weaker(self) -> None:
        comparisons = build_sister_comparisons([
            colony_score("SITE:L", "L", -5.0),
            colony_score("SITE:R", "R", 1.0),
        ])

        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0].weaker_side, "L")
        self.assertIn("left colony", comparisons[0].summary)
        self.assertEqual(comparisons[0].metric_comparisons[0].metric, "weight_pct_change")

    def test_sister_comparison_uses_current_weight_as_strength_signal(self) -> None:
        comparisons = build_sister_comparisons([
            colony_score("SITE:L", "L", -4.0, latest_weight_kg=50.0),
            colony_score("SITE:R", "R", -1.0, latest_weight_kg=20.0),
        ])

        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0].weaker_side, "R")
        self.assertEqual(comparisons[0].metric_comparisons[0].metric, "latest_weight_kg")
        self.assertIn("left colony has significant negative weight movement", comparisons[0].summary)


if __name__ == "__main__":
    unittest.main()
