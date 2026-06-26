from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from beemon_scoring.events import (
    MIN_SEGMENT_HOURS,
    detect_weight_events,
    segment_readings,
)
from beemon_scoring.features import _segmented_weight_trend, daily_weight_pct_changes
from beemon_scoring.models import SensorReading
from beemon_scoring.quality import filter_quality_issues


TZ = ZoneInfo("America/New_York")
BASE = datetime(2026, 6, 18, 11, tzinfo=TZ)


def reading(
    hours: float,
    weight: float,
    temp: float = 94.0,
    humidity: float = 55.0,
    hive_id: str = "HIVE",
    side: str = "R",
) -> SensorReading:
    observed_at = BASE + timedelta(hours=hours)
    return SensorReading(
        hive_id=hive_id,
        region_id="region_a",
        colony_side=side,
        device_uid="device",
        timestamp=int(observed_at.timestamp()),
        observed_at=observed_at,
        weight_kg=weight,
        internal_temp_f=temp,
        internal_humidity_pct=humidity,
        external_temp_f=70.0,
        external_humidity_pct=60.0,
    )


def flat_series(start_hour: float, count: int, weight: float, drift: float = 0.0) -> list[SensorReading]:
    """Hourly readings hovering around ``weight`` with a tiny per-hour drift."""
    return [reading(start_hour + i, weight + i * drift) for i in range(count)]


class WeightEventDetectionTests(unittest.TestCase):
    def test_clean_harvest_is_detected_and_splits_the_window(self) -> None:
        # 40 kg for two days, harvested down to 30 kg, then 30 kg for two days.
        readings = flat_series(0, 48, 40.0, drift=0.02) + flat_series(48, 48, 30.0, drift=0.02)

        events = detect_weight_events(readings)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "harvest")
        self.assertLess(events[0].delta_kg, 0)

        segments = segment_readings(readings, events)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].readings[0].weight_kg, 40.0)
        self.assertAlmostEqual(segments[1].readings[0].weight_kg, 30.0)

    def test_harvest_trend_excludes_the_step_and_reflects_organic_change(self) -> None:
        # Both segments gain ~1 kg organically; the -10 kg harvest must not
        # appear in the net delta or drag the slope negative.
        readings = flat_series(0, 48, 40.0, drift=0.02) + flat_series(48, 48, 30.0, drift=0.02)

        events = detect_weight_events(readings)
        trend = _segmented_weight_trend(segment_readings(readings, events))

        # Each segment rises ~0.94 kg, so the net organic change is ~+1.9 kg,
        # NOT the -10 kg the naive first-vs-last metric would have reported.
        self.assertGreater(trend["delta_kg"], 0)
        self.assertLess(abs(trend["delta_kg"]), 3.0)
        self.assertGreater(trend["slope_kg_per_day"], 0)

    def test_naive_metric_would_have_been_badly_wrong(self) -> None:
        # Documents the bug this fix addresses: first-vs-last across a harvest
        # reports a large false loss.
        readings = flat_series(0, 48, 40.0, drift=0.02) + flat_series(48, 48, 30.0, drift=0.02)
        naive_delta = readings[-1].weight_kg - readings[0].weight_kg
        self.assertLess(naive_delta, -9.0)

        trend = _segmented_weight_trend(segment_readings(readings, detect_weight_events(readings)))
        self.assertGreater(trend["delta_kg"], naive_delta + 9.0)

    def test_supering_addition_is_detected_as_addition(self) -> None:
        # A super/feeder added: a sharp sustained gain.
        readings = flat_series(0, 48, 25.0, drift=0.01) + flat_series(48, 48, 36.0, drift=0.01)
        events = detect_weight_events(readings)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "addition")
        self.assertGreater(events[0].delta_kg, 0)

    def test_drop_with_thermal_disturbance_classifies_as_swarm(self) -> None:
        # A weight drop accompanied by a large internal-temperature swing leans
        # swarm rather than harvest.
        before = flat_series(0, 48, 40.0)
        after = [reading(48 + i, 31.0, temp=78.0) for i in range(48)]
        events = detect_weight_events(before + after)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "swarm")

    def test_normal_week_has_no_events_and_one_segment(self) -> None:
        # A steadily gaining week must not trip the detector; behaviour is
        # identical to the pre-fix single-regression path.
        readings = flat_series(0, 168, 30.0, drift=0.03)
        events = detect_weight_events(readings)
        self.assertEqual(events, [])

        segments = segment_readings(readings, events)
        self.assertEqual(len(segments), 1)
        trend = _segmented_weight_trend(segments)
        # Whole-window slope over a single segment equals the plain regression.
        self.assertAlmostEqual(trend["delta_kg"], readings[-1].weight_kg - readings[0].weight_kg, places=6)

    def test_transient_dip_that_recovers_is_not_an_event(self) -> None:
        # One reading craters then immediately returns: a sensor glitch, not a
        # harvest. The coalescer must net it out to nothing.
        readings = flat_series(0, 24, 40.0) + [reading(24, 30.0)] + flat_series(25, 24, 40.0)
        events = detect_weight_events(readings)
        self.assertEqual(events, [])

    def test_drop_then_settle_collapses_to_one_event(self) -> None:
        # Real scales often drop hard then settle part-way back within an hour.
        # That bounce must read as ONE harvest, not a harvest plus an addition,
        # and the boundary must land on the drop so the step stays out of the
        # trend.
        readings = (
            flat_series(0, 48, 47.0)
            + [reading(48, 32.0)]
            + flat_series(49, 47, 35.0)
        )
        events = detect_weight_events(readings)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "harvest")

        segments = segment_readings(readings, events)
        self.assertEqual(len(segments), 2)
        # Post-event segment starts at the settled level, around 32-35 kg.
        self.assertLess(segments[1].readings[0].weight_kg, 40.0)

    def test_zero_weight_dropout_does_not_create_an_event(self) -> None:
        readings = flat_series(0, 24, 40.0) + [reading(24, 0.0)] + flat_series(25, 24, 40.0)
        # The detector ignores zero-weight pairs outright.
        self.assertEqual(detect_weight_events(readings), [])

    def test_quality_filter_keeps_confirmed_harvest_reading(self) -> None:
        # The harvest reading would historically be excluded as a "sudden jump".
        # It must now survive filtering so segmentation can use it.
        readings = flat_series(0, 48, 47.0) + flat_series(48, 48, 32.0)
        kept, _, _ = filter_quality_issues(readings)
        harvest_time = readings[48].observed_at
        self.assertIn(harvest_time, {item.observed_at for item in kept})

    def test_daily_changes_skip_event_days(self) -> None:
        # The poor-/favorable-weather metric must not count a harvest day's
        # intraday plunge as weather-driven loss.
        from datetime import date

        readings = flat_series(0, 48, 40.0, drift=0.02) + flat_series(48, 48, 30.0, drift=0.02)
        events = detect_weight_events(readings)
        event_dates = {event.observed_at.date() for event in events}
        day_types = {
            (BASE + timedelta(hours=h)).date(): "poor" for h in range(0, 96, 24)
        }

        with_skip = daily_weight_pct_changes(readings, day_types, "poor", event_dates)
        without_skip = daily_weight_pct_changes(readings, day_types, "poor")
        # Skipping the event day removes the spurious large-loss entry.
        self.assertLess(len(with_skip), len(without_skip))
        self.assertTrue(all(abs(change) < 5.0 for change in with_skip))

    def test_short_post_event_segment_is_merged(self) -> None:
        # A harvest near the very end of the window leaves too little data to
        # fit its own trend; that sliver folds into the previous segment.
        tail_hours = int(MIN_SEGMENT_HOURS) - 2
        readings = flat_series(0, 80, 40.0) + flat_series(80, tail_hours, 30.0)
        events = detect_weight_events(readings)
        self.assertEqual(len(events), 1)
        segments = segment_readings(readings, events)
        self.assertEqual(len(segments), 1)


if __name__ == "__main__":
    unittest.main()
