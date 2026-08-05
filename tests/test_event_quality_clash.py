"""Regression tests for the event-detection vs. data-quality integration.

detect_site_events is the single source of truth for weight events: the quality
filter and the feature builder both consume it, so a confirmed event is exempt
from the data-quality jump checks and reaches the feature builder identically.
These tests pin that wiring. All timestamps are hardcoded; no system clock is
read. SensorReading objects are built complete with all fields.

(Corroboration behaviour is tested separately in test_corroboration.py; these
tests use single, unambiguous floor-clearing events so they isolate the
quality/feature wiring rather than the detector's floors.)
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from beemon_scoring.events import detect_site_events, detect_weight_events
from beemon_scoring.features import build_features
from beemon_scoring.models import SensorReading
from beemon_scoring.quality import filter_quality_issues

TZ = ZoneInfo("America/New_York")

VISIT_AT = datetime(2026, 7, 10, 9, 0, 0, tzinfo=TZ)


def _reading(
    observed_at: datetime,
    weight_kg: float,
    hive_id: str = "HIVE",
    side: str = "L",
    temp_f: float = 94.0,
    humidity_pct: float = 55.0,
) -> SensorReading:
    return SensorReading(
        hive_id=hive_id,
        region_id="region_test",
        colony_side=side,
        device_uid="device",
        timestamp=int(observed_at.timestamp()),
        observed_at=observed_at,
        weight_kg=weight_kg,
        internal_temp_f=temp_f,
        internal_humidity_pct=humidity_pct,
        external_temp_f=70.0,
        external_humidity_pct=60.0,
    )


def _series(
    start: datetime,
    count: int,
    weight: float,
    drift: float = 0.0,
    jitter: float = 0.05,
    side: str = "L",
    hive_id: str = "HIVE",
) -> list[SensorReading]:
    """Hourly readings with a little sinusoidal jitter so consecutive readings
    are not identical. Jitter stays well under the hard event floors, so a plain
    baseline never produces an event."""
    return [
        _reading(
            start + timedelta(hours=i),
            weight + i * drift + jitter * math.sin(i),
            hive_id=hive_id,
            side=side,
        )
        for i in range(count)
    ]


class TestSubFloorBurstIsNotAnEvent(unittest.TestCase):
    """A single-side foraging burst below ADDITION_FLOOR_KG must not be detected.
    This is the false-positive guard the 3.0 kg addition floor exists for: the
    WTG_HSCHL burst shape (+2.7 kg, ~+11.5%) persists past the confirmation
    window yet is not a management event."""

    def test_sub_floor_burst_produces_no_event(self):
        base = VISIT_AT - timedelta(hours=20)
        pre = _series(base, 19, 23.5, side="L", hive_id="BURST")
        before = _reading(VISIT_AT - timedelta(hours=1), 23.5, hive_id="BURST", side="L")
        burst = _reading(VISIT_AT, 23.5 + 2.7, hive_id="BURST", side="L")
        post = _series(VISIT_AT + timedelta(hours=1), 20, 23.5 + 2.7, drift=-0.01, side="L", hive_id="BURST")
        readings = pre + [before, burst] + post

        self.assertEqual(detect_weight_events(readings), [], "2.7 kg burst is below ADDITION_FLOOR_KG")


class TestQualityFilterExemptsEvent(unittest.TestCase):
    """A confirmed event reading carrying an internal-temperature excursion must
    survive the quality filter: because filter_quality_issues consumes the same
    detect_site_events output, the event reading is exempt from the jump checks
    that would otherwise exclude it on the 30 F swing."""

    def test_event_reading_survives_temp_excursion(self):
        pre = _series(VISIT_AT - timedelta(hours=20), 19, 45.0, side="L", hive_id="EVT")
        before = _reading(VISIT_AT - timedelta(hours=1), 45.0, hive_id="EVT", side="L", temp_f=92.0)
        # Real +6 kg addition, with a 92 F -> 62 F excursion on the event reading.
        event = _reading(VISIT_AT, 51.0, hive_id="EVT", side="L", temp_f=62.0)
        post = _series(VISIT_AT + timedelta(hours=1), 20, 51.0, side="L", hive_id="EVT")
        readings = pre + [before, event] + post

        events_by_colony = detect_site_events(readings)
        self.assertEqual(len(events_by_colony["EVT:L"]), 1, "the +6 kg addition must be detected")

        filtered, quality_by_colony, _meta = filter_quality_issues(readings, events_by_colony)

        survived = [r for r in filtered if r.colony_id == "EVT:L" and r.observed_at == VISIT_AT]
        self.assertEqual(len(survived), 1, "event reading must survive filtering")

        visit_iso = VISIT_AT.isoformat()
        attributable = [m for m in quality_by_colony.get("EVT:L", []) if visit_iso in m]
        self.assertEqual(attributable, [], "no quality issue should reference the event timestamp")


class TestEventReachesFeatures(unittest.TestCase):
    """The same event, wired end to end (detect_site_events ->
    filter_quality_issues -> build_features), must reach
    ColonyFeatures.weight_event_count -- i.e. it is not silently dropped by the
    quality filter before feature building sees it."""

    def test_event_reaches_feature_weight_event_count(self):
        pre = _series(VISIT_AT - timedelta(hours=20), 19, 45.0, side="L", hive_id="E2E")
        before = _reading(VISIT_AT - timedelta(hours=1), 45.0, hive_id="E2E", side="L", temp_f=92.0)
        event = _reading(VISIT_AT, 51.0, hive_id="E2E", side="L", temp_f=62.0)
        post = _series(VISIT_AT + timedelta(hours=1), 20, 51.0, side="L", hive_id="E2E")
        readings = pre + [before, event] + post

        events_by_colony = detect_site_events(readings)
        filtered, quality_by_colony, _meta = filter_quality_issues(readings, events_by_colony)
        features = build_features(filtered, {}, {}, quality_by_colony, events_by_colony=events_by_colony)

        feature = next(f for f in features if f.colony_id == "E2E:L")
        self.assertGreaterEqual(feature.weight_event_count, 1, "event must survive end-to-end into features")


class TestSettleWindowExemptsPostEventDisturbance(unittest.TestCase):
    """A management visit disturbs the internal-temp sensor for several hours,
    not just the event reading. EVENT_SETTLE_WINDOW_HOURS exempts the readings
    immediately after the event so the disturbed run is not excluded (the
    PRT_1:L shape)."""

    def test_confirmed_addition_disturbed_temp_produces_zero_exclusions(self):
        event_at = VISIT_AT
        pre = _series(event_at - timedelta(hours=20), 19, 61.0, side="L", hive_id="PRT")
        before = _reading(event_at - timedelta(hours=1), 61.0, hive_id="PRT", side="L")
        event = _reading(event_at, 68.0, hive_id="PRT", side="L", temp_f=94.0)
        disturbed = [
            _reading(event_at + timedelta(hours=i), 68.0 + 0.05 * math.sin(i), hive_id="PRT", side="L", temp_f=60.0)
            for i in range(1, 7)
        ]
        post = _series(event_at + timedelta(hours=7), 14, 68.0, side="L", hive_id="PRT")

        readings = pre + [before, event] + disturbed + post
        events = detect_weight_events(readings)
        self.assertEqual(len(events), 1, "the +7 kg addition must be confirmed standalone")

        events_by_colony = {"PRT:L": events}
        filtered, _quality_by_colony, meta = filter_quality_issues(readings, events_by_colony)

        filtered_times = {r.observed_at for r in filtered}
        visit_times = {r.observed_at for r in [event] + disturbed}
        self.assertTrue(visit_times.issubset(filtered_times), "no visit or post-visit reading should be excluded")
        self.assertEqual(meta["excluded_sensor_reading_count"], 0)


if __name__ == "__main__":
    unittest.main()
