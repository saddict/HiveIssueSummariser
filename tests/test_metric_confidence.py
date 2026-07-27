"""Per-metric peer-pool confidence labels (PROJECT_HANDOFF section 16 residual risk).

A metric's eligible peer pool can shrink below the region size via min_sample_attr
gating (the weather metrics). At n<=2 Samuelson's inequality pins |badness_z| at
the sqrt(n-1) bound, so the z carries only sign, not magnitude. These tests pin
that such a comparison is labelled confidence="low", that a pinned n=3 pool is
also flagged, that a low-confidence flag does not change status, and that the text
report surfaces the marker.
"""
from __future__ import annotations

import dataclasses
import unittest

from beemon_scoring.reporting import build_text_report
from beemon_scoring.scoring import _score_features, _status

from test_scoring_logic import feature


SETTINGS = {"zscore_badness_threshold": 1.0, "weight_drop_pct_threshold": 5.0}

# Minimal metadata satisfying the keys build_text_report reads.
REPORT_METADATA = {
    "start_at": "2026-06-11T00:00:00-04:00",
    "end_at": "2026-06-18T00:00:00-04:00",
    "window_days": 7,
    "colony_count": 4,
    "min_colony_days_observed": 7.0,
    "max_colony_days_observed": 7.0,
    "valid_sensor_reading_count": 100,
    "sensor_reading_count": 100,
    "excluded_sensor_reading_count": 0,
    "data_quality_issue_count": 0,
}


def _with_favorable_slope(colony_feature, slope: float):
    """features from the shared helper are frozen; copy with a new slope."""
    return dataclasses.replace(colony_feature, favorable_weather_weight_slope_pct_per_day=slope)


def _comparison(score, metric_name):
    return next(c for c in score.comparisons if c.metric == metric_name)


class MetricConfidenceTests(unittest.TestCase):
    def test_weather_metric_with_two_eligible_peers_is_low_confidence(self) -> None:
        # Four colonies in one region, but only two have a favorable window, so
        # the favorable-weather metric is scored against n=2 while the ungated
        # metrics see all four.
        features = [
            # Differentiate the favorable-weather values so the pool is
            # non-degenerate in value but still degenerate in size (n=2).
            _with_favorable_slope(feature("A:L", favorable_windows=1, poor_windows=1, latest_weight_kg=10.0), -2.0),
            _with_favorable_slope(feature("B:L", favorable_windows=1, poor_windows=1, latest_weight_kg=20.0), 3.0),
            feature("C:L", favorable_windows=0, poor_windows=1, latest_weight_kg=30.0),
            feature("D:L", favorable_windows=0, poor_windows=1, latest_weight_kg=40.0),
        ]

        scores = _score_features(features, SETTINGS)
        a = next(s for s in scores if s.colony_id == "A:L")

        favorable = _comparison(a, "favorable_weather_weight_slope_pct_per_day")
        self.assertEqual(favorable.peer_count, 2)
        self.assertAlmostEqual(favorable.z_bound, 1.0)
        self.assertEqual(favorable.confidence, "low")

        weight = _comparison(a, "latest_weight_kg")
        self.assertEqual(weight.peer_count, 4)
        self.assertEqual(weight.confidence, "normal")

    def test_pinned_zscore_in_three_peer_pool_is_low_confidence(self) -> None:
        # n=3 pool where one colony is an extreme outlier drives |z| to the
        # sqrt(2) bound, saturating the magnitude even though n>2.
        features = [
            feature("A:L", favorable_windows=1, poor_windows=1, latest_weight_kg=1.0),
            feature("B:L", favorable_windows=1, poor_windows=1, latest_weight_kg=1.0),
            feature("C:L", favorable_windows=1, poor_windows=1, latest_weight_kg=100.0),
        ]

        scores = _score_features(features, SETTINGS)
        c = next(s for s in scores if s.colony_id == "C:L")
        weight = _comparison(c, "latest_weight_kg")

        self.assertEqual(weight.peer_count, 3)
        self.assertAlmostEqual(weight.z_bound, 2.0 ** 0.5, places=6)
        self.assertGreaterEqual(abs(weight.badness_z), 0.999 * weight.z_bound)
        self.assertEqual(weight.confidence, "low")

    def test_low_confidence_flag_does_not_change_status(self) -> None:
        # A low-confidence flag must not push a colony to watch/underperforming.
        flags = [
            "Low confidence: favorable-weather weight percent trend compared "
            "against only 2 eligible peers (z-score bounded at ±1.0)."
        ]
        self.assertEqual(_status(0.0, flags), "normal")
        # A real performance flag still triggers watch.
        self.assertEqual(
            _status(0.0, flags + ["temperature instability is 1.2 standard deviations worse than peers."]),
            "watch",
        )

    def test_text_report_marks_low_confidence_driver(self) -> None:
        features = [
            _with_favorable_slope(feature("A:L", favorable_windows=1, poor_windows=1, latest_weight_kg=10.0), -50.0),
            _with_favorable_slope(feature("B:L", favorable_windows=1, poor_windows=1, latest_weight_kg=20.0), 3.0),
            feature("C:L", favorable_windows=0, poor_windows=1, latest_weight_kg=30.0),
            feature("D:L", favorable_windows=0, poor_windows=1, latest_weight_kg=40.0),
        ]

        scores = _score_features(features, SETTINGS)
        report = build_text_report(scores, REPORT_METADATA)

        self.assertIn("low confidence: n=2 peers", report)


if __name__ == "__main__":
    unittest.main()
