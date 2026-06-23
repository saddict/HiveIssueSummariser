from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from beemon_scoring.data_loader import _coordinate_region_ids
from beemon_scoring.features import daily_weight_pct_changes
from beemon_scoring.metrics import Metric
from beemon_scoring.models import ColonyFeatures, ColonyScore, MetricComparison, SensorReading
from beemon_scoring.reporting import build_region_summaries
from beemon_scoring.scoring import _eligible_metric_peers, _score_features
from beemon_scoring.sister_comparison import build_sister_comparisons


TZ = ZoneInfo("America/New_York")


def reading(hive_id: str, side: str, timestamp: int, weight: float, observed_at: datetime, region_id: str = "region_a") -> SensorReading:
    return SensorReading(
        hive_id=hive_id,
        region_id=region_id,
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


def feature(
    colony_id: str,
    favorable_windows: int,
    poor_windows: int,
    region_id: str = "region_a",
    latest_weight_kg: float = 10.0,
    weight_pct_change: float = 0.0,
) -> ColonyFeatures:
    return ColonyFeatures(
        colony_id=colony_id,
        region_id=region_id,
        hive_id=colony_id.split(":")[0],
        colony_side=colony_id.split(":")[1],
        sample_count=10,
        excluded_reading_count=0,
        data_quality_flags=[],
        start_at=datetime(2026, 6, 11, tzinfo=TZ),
        end_at=datetime(2026, 6, 18, tzinfo=TZ),
        days_observed=7.0,
        latest_weight_kg=latest_weight_kg,
        weight_delta_kg=0.0,
        weight_pct_change=weight_pct_change,
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


def colony_score(
    colony_id: str,
    side: str,
    weight_pct_change: float,
    latest_weight_kg: float = 10.0,
    region_id: str = "region_a",
    score: float = 0.0,
    status: str = "normal",
) -> ColonyScore:
    base = feature(
        colony_id,
        favorable_windows=1,
        poor_windows=1,
        region_id=region_id,
        latest_weight_kg=latest_weight_kg,
        weight_pct_change=weight_pct_change,
    )
    return ColonyScore(
        colony_id=colony_id,
        region_id=region_id,
        hive_id=colony_id.split(":")[0],
        colony_side=side,
        score=score,
        status=status,
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
            ),
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

        changes = daily_weight_pct_changes(readings, day_types, "favorable")

        self.assertEqual(len(changes), 2)
        self.assertAlmostEqual(changes[0], 1.0)
        self.assertAlmostEqual(changes[1], -0.990099, places=5)

    def test_weather_metric_peer_eligibility_requires_matching_windows(self) -> None:
        features = [
            feature("A:L", favorable_windows=1, poor_windows=0),
            feature("B:L", favorable_windows=0, poor_windows=1),
            feature("C:L", favorable_windows=2, poor_windows=1),
        ]
        metric = Metric(
            name="favorable_weather_weight_slope_pct_per_day",
            label="favorable-weather weight percent trend",
            direction="higher_is_better",
            weight=0.06,
            unit="%/day",
            min_sample_attr="favorable_weather_window_count",
            min_sample_count=1,
        )

        eligible = _eligible_metric_peers(features, metric)

        self.assertEqual([item.colony_id for item in eligible], ["A:L", "C:L"])

    def test_coordinate_regions_group_sites_within_10_miles(self) -> None:
        region_ids = _coordinate_region_ids(
            {
                "DR_WLKS": {"latitude": 36.247479, "longitude": -81.90097},
                "6LR": {"latitude": 36.212598, "longitude": -81.679678},
                "PRT_1": {"latitude": 36.203096, "longitude": -81.625792},
                "WTG_HSCHL": {"latitude": 36.214155, "longitude": -81.649756},
            },
            10.0,
        )

        self.assertEqual(len(set(region_ids.values())), 2)
        self.assertEqual(region_ids["6LR"], region_ids["PRT_1"])
        self.assertEqual(region_ids["6LR"], region_ids["WTG_HSCHL"])
        self.assertNotEqual(region_ids["DR_WLKS"], region_ids["6LR"])

    def test_region_scoring_uses_only_same_region_peers(self) -> None:
        settings = {"zscore_badness_threshold": 1.0, "weight_drop_pct_threshold": 5.0}
        features = [
            feature("A:L", favorable_windows=1, poor_windows=1, region_id="north", latest_weight_kg=10.0),
            feature("B:L", favorable_windows=1, poor_windows=1, region_id="north", latest_weight_kg=20.0),
            feature("C:L", favorable_windows=1, poor_windows=1, region_id="south", latest_weight_kg=100.0),
            feature("D:L", favorable_windows=1, poor_windows=1, region_id="south", latest_weight_kg=110.0),
        ]

        scores = _score_features(features, settings)

        north_score = next(score for score in scores if score.colony_id == "A:L")
        south_score = next(score for score in scores if score.colony_id == "C:L")
        north_weight = next(comparison for comparison in north_score.comparisons if comparison.metric == "latest_weight_kg")
        south_weight = next(comparison for comparison in south_score.comparisons if comparison.metric == "latest_weight_kg")

        self.assertAlmostEqual(north_weight.peer_mean, 15.0)
        self.assertAlmostEqual(south_weight.peer_mean, 105.0)

    def test_region_summaries_surface_strong_and_weak_colonies(self) -> None:
        summaries = build_region_summaries([
            colony_score("WTG_HSCHL:L", "L", -1.5, latest_weight_kg=28.1, region_id="high_country_nc", score=28.1, status="underperforming"),
            colony_score("WTG_HSCHL:R", "R", -7.3, latest_weight_kg=24.2, region_id="high_country_nc", score=47.3, status="underperforming"),
            colony_score("6LR:L", "L", -1.8, latest_weight_kg=49.1, region_id="high_country_nc", score=3.7, status="normal"),
            colony_score("6LR:R", "R", 2.0, latest_weight_kg=37.9, region_id="high_country_nc", score=0.7, status="normal"),
        ])

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.region_id, "high_country_nc")
        self.assertEqual([item.colony_id for item in summary.performing_well_colonies], ["6LR:R", "6LR:L"])
        self.assertEqual([item.colony_id for item in summary.underperforming_colonies], ["WTG_HSCHL:R", "WTG_HSCHL:L"])
        self.assertIn("WTG_HSCHL:R", summary.summary)

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
