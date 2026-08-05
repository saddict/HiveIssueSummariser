"""Tests for corroboration-as-confidence (events.py _tag_corroboration).

Corroboration never detects or promotes an event; it only tags an
independently detected event ``corroborated`` when the sister colony also has a
floor-clearing event within CORROBORATION_WINDOW_HOURS. The tagging is
direction-agnostic on purpose: a joint harvest, a joint supering, and an
equalisation (harvest one side, add to the other in the same visit) are all one
apiary visit. All timestamps are hardcoded; no system clock is read.
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from beemon_scoring.events import detect_site_events
from beemon_scoring.models import SensorReading

TZ = ZoneInfo("America/New_York")
VISIT_AT = datetime(2026, 7, 10, 9, 0, 0, tzinfo=TZ)


def _reading(observed_at, weight_kg, hive_id, side, temp_f=94.0, humidity_pct=55.0):
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


def _series(start, count, weight, drift=0.0, jitter=0.05, side="L", hive_id="HIVE"):
    return [
        _reading(start + timedelta(hours=i), weight + i * drift + jitter * math.sin(i), hive_id, side)
        for i in range(count)
    ]


def _step_colony(hive_id, side, base_kg, step_kg, event_at=VISIT_AT):
    """A colony with one clean floor-clearing step at ``event_at`` that persists."""
    pre = _series(event_at - timedelta(hours=20), 19, base_kg, side=side, hive_id=hive_id)
    before = _reading(event_at - timedelta(hours=1), base_kg, hive_id, side)
    event = _reading(event_at, base_kg + step_kg, hive_id, side)
    post = _series(event_at + timedelta(hours=1), 20, base_kg + step_kg, side=side, hive_id=hive_id)
    return pre + [before, event] + post


class TestCorroborationTagging(unittest.TestCase):
    def test_same_direction_visit_tags_both_corroborated(self):
        # Both sisters get a large addition at the same visit (joint supering).
        readings = _step_colony("JOINT", "L", 45.0, 6.0) + _step_colony("JOINT", "R", 44.0, 5.0)
        events = detect_site_events(readings)
        self.assertEqual(len(events["JOINT:L"]), 1)
        self.assertEqual(len(events["JOINT:R"]), 1)
        self.assertTrue(events["JOINT:L"][0].corroborated, "L addition corroborated by R")
        self.assertTrue(events["JOINT:R"][0].corroborated, "R addition corroborated by L")

    def test_equalisation_opposite_directions_still_corroborated(self):
        # L is harvested, R is added to, in the same visit -- direction-agnostic.
        readings = _step_colony("EQ", "L", 60.0, -6.0) + _step_colony("EQ", "R", 44.0, 6.0)
        events = detect_site_events(readings)
        self.assertEqual(len(events["EQ:L"]), 1)
        self.assertEqual(len(events["EQ:R"]), 1)
        self.assertLess(events["EQ:L"][0].delta_kg, 0, "L is a harvest")
        self.assertGreater(events["EQ:R"][0].delta_kg, 0, "R is an addition")
        self.assertTrue(events["EQ:L"][0].corroborated, "opposite-direction sister still corroborates")
        self.assertTrue(events["EQ:R"][0].corroborated)

    def test_solo_event_is_isolated(self):
        # Only L moves; R stays flat. L's event is real but uncorroborated.
        flat_r = _series(VISIT_AT - timedelta(hours=20), 41, 44.0, side="R", hive_id="SOLO")
        readings = _step_colony("SOLO", "L", 45.0, 6.0) + flat_r
        events = detect_site_events(readings)
        self.assertEqual(len(events["SOLO:L"]), 1)
        self.assertEqual(events["SOLO:R"], [])
        self.assertFalse(events["SOLO:L"][0].corroborated, "no sister event -> isolated")

    def test_events_far_apart_are_not_corroborated(self):
        # L moves at the visit; R moves two days later -- outside the window.
        r_late = VISIT_AT + timedelta(days=2)
        readings = _step_colony("FAR", "L", 45.0, 6.0) + _step_colony("FAR", "R", 44.0, 6.0, event_at=r_late)
        events = detect_site_events(readings)
        self.assertFalse(events["FAR:L"][0].corroborated)
        self.assertFalse(events["FAR:R"][0].corroborated)

    def test_single_side_site_does_not_crash(self):
        readings = _step_colony("LONE", "L", 45.0, 6.0)
        events = detect_site_events(readings)
        self.assertEqual(len(events["LONE:L"]), 1)
        self.assertFalse(events["LONE:L"][0].corroborated, "no sister -> cannot be corroborated")


if __name__ == "__main__":
    unittest.main()
