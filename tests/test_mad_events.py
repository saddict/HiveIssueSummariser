"""Regression tests for the MAD-based weight event detector and sister corroboration.

All timestamps are hardcoded. No system clock is read.
SensorReading objects are built complete with all 11 fields.
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from beemon_scoring.events import (
    corroborate_sister_events,
    detect_weight_events,
    segment_readings,
)
from beemon_scoring.models import SensorReading

TZ = ZoneInfo("America/New_York")

# 2026-07-07 17:00 EDT = 21:00 UTC — the confirmed 6LR harvest timestamp.
HARVEST_AT = datetime(2026, 7, 7, 17, 0, 0, tzinfo=TZ)


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
    """Hourly readings with sinusoidal jitter so consecutive-pair deltas are all
    distinct and MAD is non-zero. Without variation all deltas are identical,
    MAD = 0, and the robust-z detector is disabled."""
    return [
        _reading(
            start + timedelta(hours=i),
            weight + i * drift + jitter * math.sin(i),
            hive_id=hive_id,
            side=side,
        )
        for i in range(count)
    ]


class TestMADDetector(unittest.TestCase):

    def test_real_harvest_6lr_l_detected_at_exact_timestamp(self):
        # Mirrors the 6LR:L confirmed harvest: 61.249 → 57.209 kg at 2026-07-07
        # 17:00 EDT (21:00 UTC). Pre/post segments supply the MAD calibration;
        # the drop must produce exactly one harvest event at that timestamp.
        pre = _series(HARVEST_AT - timedelta(hours=20), 19, 61.0, drift=0.01)
        before = _reading(HARVEST_AT - timedelta(hours=1), 61.249)
        harvest = _reading(HARVEST_AT, 57.209)
        post = _series(HARVEST_AT + timedelta(hours=1), 20, 57.21, drift=0.005)

        readings = pre + [before, harvest] + post
        events = detect_weight_events(readings)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "harvest")
        self.assertEqual(events[0].observed_at, HARVEST_AT)
        self.assertAlmostEqual(events[0].delta_kg, 57.209 - 61.249, places=2)

    def test_normal_foraging_week_produces_no_events_and_one_segment(self):
        # A steadily gaining week with sinusoidal variation must not trigger the
        # detector: ordinary foraging peaks stay well below MAD_SENSITIVITY_K.
        readings = _series(HARVEST_AT - timedelta(hours=167), 168, 40.0, drift=0.02)
        events = detect_weight_events(readings)
        self.assertEqual(events, [])
        self.assertEqual(len(segment_readings(readings, events)), 1)

    def test_corroboration_promotes_sister_sub_threshold_drop(self):
        # L: confirmed harvest (z >> MAD_SENSITIVITY_K) at HARVEST_AT.
        # R: sub-threshold drop (MAD_CORROBORATE_K <= |z| < MAD_SENSITIVITY_K)
        #    at the same timestamp — should be promoted via corroboration.
        #
        # Important: both series are built without a discontinuity between the
        # pre-harvest readings and the event reading. A jump from the pre period
        # to a manually-set "before" weight fires a second MAD candidate, gets
        # coalesced into a 2-element cluster with the harvest, and _coalesce_events
        # then applies the MIN_EVENT_DROP_PCT floor to the cluster net delta —
        # which can drop the cluster even though each individual step is large.
        base = HARVEST_AT - timedelta(hours=20)

        # L: pre-harvest series ends at HARVEST_AT-1h (~60 kg); harvest drops to
        # 54 kg (-6 kg ≈ -10 %, clearing both MIN_EVENT_DROP_KG and _PCT).
        l_pre = _series(base, 20, 60.0, side="L", hive_id="SITE")
        l_harvest = _reading(HARVEST_AT, 54.0, hive_id="SITE", side="L")
        l_post = _series(HARVEST_AT + timedelta(hours=1), 20, 54.0, side="L", hive_id="SITE")
        l_readings = l_pre + [l_harvest] + l_post

        # R: small drop (~-0.2 kg, |z| ≈ 4.4 — above corroborate K, below sensitivity K).
        r_pre = _series(base, 20, 50.0, side="R", hive_id="SITE")
        r_drop = _reading(HARVEST_AT, 49.8, hive_id="SITE", side="R")
        r_post = _series(HARVEST_AT + timedelta(hours=1), 20, 49.8, side="R", hive_id="SITE")
        r_readings = r_pre + [r_drop] + r_post

        l_events = detect_weight_events(l_readings)
        r_events = detect_weight_events(r_readings)

        # Preconditions: L fires, R does not.
        self.assertEqual(len(l_events), 1, "L harvest must be confirmed standalone")
        self.assertEqual(r_events, [], "R must not fire standalone (|z| < MAD_SENSITIVITY_K)")

        result = corroborate_sister_events(
            {"L": l_events, "R": r_events},
            {"L": l_readings, "R": r_readings},
        )

        self.assertEqual(len(result["L"]), 1, "L event list unchanged")
        self.assertEqual(len(result["R"]), 1, "R drop must be promoted")
        self.assertIn("sister-corroborated", result["R"][0].kind)
        self.assertEqual(result["R"][0].observed_at, HARVEST_AT)
        self.assertLess(result["R"][0].delta_kg, 0, "Promoted event is a drop")

    def test_corroboration_inverse_no_promotion_without_sister_event(self):
        # R has the SAME sub-threshold drop as the positive test above, but L
        # has no event at all. Corroboration must NOT promote R — it is not a
        # backdoor threshold reduction. Promotion requires a confirmed sister event.
        base = HARVEST_AT - timedelta(hours=20)

        l_readings = _series(base, 41, 60.0, side="L", hive_id="SITE")

        r_pre = _series(base, 20, 50.0, side="R", hive_id="SITE")
        r_drop = _reading(HARVEST_AT, 49.8, hive_id="SITE", side="R")
        r_post = _series(HARVEST_AT + timedelta(hours=1), 20, 49.8, side="R", hive_id="SITE")
        r_readings = r_pre + [r_drop] + r_post

        l_events = detect_weight_events(l_readings)
        r_events = detect_weight_events(r_readings)
        self.assertEqual(l_events, [])
        self.assertEqual(r_events, [])

        result = corroborate_sister_events(
            {"L": l_events, "R": r_events},
            {"L": l_readings, "R": r_readings},
        )

        self.assertEqual(result["R"], [], "R must not be promoted without a confirmed sister event")

    def test_degenerate_too_few_readings_no_events(self):
        # Fewer than _MIN_MAD_DELTAS usable pairs disables MAD detection;
        # even a clear step must not produce a spurious event.
        start = HARVEST_AT - timedelta(hours=5)
        readings = [_reading(start + timedelta(hours=i), 40.0) for i in range(7)]
        readings.append(_reading(start + timedelta(hours=7), 30.0))
        self.assertEqual(detect_weight_events(readings), [])

    def test_degenerate_near_flat_window_no_events(self):
        # Identical weights → MAD = 0 → robust-z undefined → no events.
        start = HARVEST_AT - timedelta(hours=19)
        readings = [_reading(start + timedelta(hours=i), 40.0) for i in range(20)]
        self.assertEqual(detect_weight_events(readings), [])


if __name__ == "__main__":
    unittest.main()
