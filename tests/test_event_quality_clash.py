"""Regression tests for the event-detection vs. data-quality clash (F1-F5).

See the "Event Detection vs. Data-Quality Clash" work order. These tests define
the acceptance criteria for Steps 2-6 of that plan. All timestamps are
hardcoded. No system clock is read. SensorReading objects are built complete
with all 11 fields, following the helper conventions in test_mad_events.py.
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from beemon_scoring.events import (
    corroborate_sister_events,
    detect_site_events,
    detect_weight_events,
)
from beemon_scoring.features import build_features
from beemon_scoring.models import SensorReading
from beemon_scoring.quality import filter_quality_issues

TZ = ZoneInfo("America/New_York")

# Arbitrary fixed visit timestamp, distinct from test_mad_events.py's HARVEST_AT.
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


class TestPairedSubThresholdAdditions(unittest.TestCase):
    """T1: targets F1+F2 - a genuine two-sister supering/feeding visit where
    each side's individual step is sub-threshold (blocked by
    MIN_CANDIDATE_ADDITION_KG) yet the paired signal is strong evidence of a
    real event. Originally: 0 events on either side, fixed by Step 3's paired
    promotion. After Step 6 (aggregate multi-step detection), R's own
    two-step move independently clears the floor over its 3-hour aggregate
    span -- a second, legitimate path to the same conclusion -- so R is no
    longer guaranteed to need corroboration; only L's single clean step still
    does. The acceptance criterion is the final corroborated result, not
    which mechanism produces it."""

    def test_paired_visit_produces_one_addition_each_side(self):
        # L: ~45 kg baseline, +6.1% (~2.745 kg) in a single hourly step at the visit.
        l_pre = _series(VISIT_AT - timedelta(hours=20), 19, 45.0, side="L", hive_id="PAIR")
        l_before = _reading(VISIT_AT - timedelta(hours=1), 45.0, hive_id="PAIR", side="L")
        l_event = _reading(VISIT_AT, 45.0 + 2.745, hive_id="PAIR", side="L")
        l_post = _series(VISIT_AT + timedelta(hours=1), 20, 47.745, side="L", hive_id="PAIR")
        l_readings = l_pre + [l_before, l_event] + l_post

        # R: ~44 kg baseline, similar gain spread over two hourly steps at the
        # same visit (+1.6 kg then +1.6 kg -- each step individually clears the
        # provisional PAIRED_MIN_GAIN_KG/PCT floors but not MIN_CANDIDATE_ADDITION_KG).
        r_pre = _series(VISIT_AT - timedelta(hours=20), 19, 44.0, side="R", hive_id="PAIR")
        r_before = _reading(VISIT_AT - timedelta(hours=1), 44.0, hive_id="PAIR", side="R")
        r_step1 = _reading(VISIT_AT, 44.0 + 1.6, hive_id="PAIR", side="R")
        r_step2 = _reading(VISIT_AT + timedelta(hours=1), 44.0 + 1.6 + 1.6, hive_id="PAIR", side="R")
        r_post = _series(VISIT_AT + timedelta(hours=2), 19, 47.2, side="R", hive_id="PAIR")
        r_readings = r_pre + [r_before, r_step1, r_step2] + r_post

        l_events = detect_weight_events(l_readings)
        r_events = detect_weight_events(r_readings)

        # Precondition: L's single step is individually sub-threshold (blocked
        # by MIN_CANDIDATE_ADDITION_KG) and, being a single isolated step, is
        # not rescued by the aggregate pass either -- it still needs
        # corroboration. (R's steps, by contrast, may now be independently
        # detected via the aggregate span -- see class docstring.)
        self.assertEqual(l_events, [], "L step (2.745 kg) is below MIN_CANDIDATE_ADDITION_KG")

        result = corroborate_sister_events(
            {"L": l_events, "R": r_events},
            {"L": l_readings, "R": r_readings},
        )

        self.assertEqual(len(result["L"]), 1, "L must have exactly one promoted addition")
        self.assertIn("addition", result["L"][0].kind)
        self.assertEqual(len(result["R"]), 1, "R must have exactly one promoted addition (coalesced)")
        self.assertIn("addition", result["R"][0].kind)


class TestForagingBurstNotPaired(unittest.TestCase):
    """T2: false-positive guard targeting F2 - only one side spikes (the
    WTG_HSCHL foraging-burst shape: +2.7 kg, ~+11.5%, decaying slowly enough to
    pass _shift_persists) while the sister moves ordinarily. Must NOT promote
    on either side, now or after Step 3's paired promotion lands (that is the
    guard proving pairing does not resurrect the false positive the 3.0 kg
    floor was tuned to kill)."""

    def test_single_side_foraging_burst_is_not_promoted(self):
        base = VISIT_AT - timedelta(hours=20)

        # L: foraging burst, 23.5 kg baseline, +2.7 kg (~11.5%) at the visit,
        # decaying only slowly afterwards (persists past the confirmation window).
        l_pre = _series(base, 19, 23.5, side="L", hive_id="BURST")
        l_before = _reading(VISIT_AT - timedelta(hours=1), 23.5, hive_id="BURST", side="L")
        l_burst = _reading(VISIT_AT, 23.5 + 2.7, hive_id="BURST", side="L")
        l_post = _series(VISIT_AT + timedelta(hours=1), 20, 23.5 + 2.7, drift=-0.01, side="L", hive_id="BURST")
        l_readings = l_pre + [l_before, l_burst] + l_post

        # R: ordinary movement, no step.
        r_readings = _series(base, 41, 22.0, side="R", hive_id="BURST")

        l_events = detect_weight_events(l_readings)
        r_events = detect_weight_events(r_readings)

        self.assertEqual(l_events, [], "L burst is below MIN_CANDIDATE_ADDITION_KG standalone")
        self.assertEqual(r_events, [], "R has no step at all")

        result = corroborate_sister_events(
            {"L": l_events, "R": r_events},
            {"L": l_readings, "R": r_readings},
        )

        self.assertEqual(result["L"], [], "L must not be promoted: R has no corroborating step")
        self.assertEqual(result["R"], [], "R must not be promoted: R never moved")


class TestQualityFilterExemptsCorroboratedEvent(unittest.TestCase):
    """T3: targets F3+F4 - the T1 visit scenario plus an internal-temperature
    excursion on the visit reading. Before Step 4, quality.py ran its own
    detect_weight_events on raw per-colony readings (never seeing the
    corroborated event), so the temperature swing excluded the visit reading
    even though the paired addition is a real, corroborated event. Passes now
    that filter_quality_issues consumes the single site-level event set from
    detect_site_events."""

    def test_visit_reading_survives_temp_excursion(self):
        l_pre = _series(VISIT_AT - timedelta(hours=20), 19, 45.0, side="L", hive_id="PAIR3")
        l_before = _reading(VISIT_AT - timedelta(hours=1), 45.0, hive_id="PAIR3", side="L")
        # Internal temp excursion at the visit reading: 92 F -> 62 F.
        l_event = _reading(VISIT_AT, 45.0 + 2.745, hive_id="PAIR3", side="L", temp_f=62.0)
        l_post = _series(VISIT_AT + timedelta(hours=1), 20, 47.745, side="L", hive_id="PAIR3")
        l_readings = [
            r if r.observed_at != VISIT_AT - timedelta(hours=1) else _reading(
                r.observed_at, r.weight_kg, hive_id="PAIR3", side="L", temp_f=92.0
            )
            for r in (l_pre + [l_before])
        ] + [l_event] + l_post

        r_pre = _series(VISIT_AT - timedelta(hours=20), 19, 44.0, side="R", hive_id="PAIR3")
        r_before = _reading(VISIT_AT - timedelta(hours=1), 44.0, hive_id="PAIR3", side="R")
        r_step1 = _reading(VISIT_AT, 44.0 + 1.6, hive_id="PAIR3", side="R")
        r_step2 = _reading(VISIT_AT + timedelta(hours=1), 44.0 + 1.6 + 1.6, hive_id="PAIR3", side="R")
        r_post = _series(VISIT_AT + timedelta(hours=2), 19, 47.2, side="R", hive_id="PAIR3")
        r_readings = r_pre + [r_before, r_step1, r_step2] + r_post

        combined = l_readings + r_readings
        events_by_colony = detect_site_events(combined)
        filtered, quality_by_colony, _meta = filter_quality_issues(combined, events_by_colony)

        filtered_l_visit = [
            r for r in filtered if r.colony_id == "PAIR3:L" and r.observed_at == VISIT_AT
        ]
        self.assertEqual(len(filtered_l_visit), 1, "L visit reading must survive filtering")

        l_messages = quality_by_colony.get("PAIR3:L", [])
        visit_iso = VISIT_AT.isoformat()
        attributable = [m for m in l_messages if visit_iso in m]
        self.assertEqual(attributable, [], "no quality issue should reference the visit timestamp")


class TestEventSurvivesEndToEnd(unittest.TestCase):
    """Step 4 acceptance: with events_by_colony wired through as the single
    source of truth (detect_site_events -> filter_quality_issues ->
    build_features), the T3 visit event -- whose step reading would
    previously have been excluded by the temp-jump filter -- now survives
    into ColonyFeatures.weight_event_count. Documents the semantic shift in
    D3: features previously only saw events detected on already-filtered
    readings; they now receive the same pre-filter, site-level event set the
    quality filter uses."""

    def test_visit_event_reaches_feature_weight_event_count(self):
        l_pre = _series(VISIT_AT - timedelta(hours=20), 19, 45.0, side="L", hive_id="PAIR4")
        l_before = _reading(VISIT_AT - timedelta(hours=1), 45.0, hive_id="PAIR4", side="L", temp_f=92.0)
        # Same internal-temp excursion as T3: 92 F -> 62 F at the visit reading.
        l_event = _reading(VISIT_AT, 45.0 + 2.745, hive_id="PAIR4", side="L", temp_f=62.0)
        l_post = _series(VISIT_AT + timedelta(hours=1), 20, 47.745, side="L", hive_id="PAIR4")
        l_readings = l_pre + [l_before, l_event] + l_post

        r_pre = _series(VISIT_AT - timedelta(hours=20), 19, 44.0, side="R", hive_id="PAIR4")
        r_before = _reading(VISIT_AT - timedelta(hours=1), 44.0, hive_id="PAIR4", side="R")
        r_step1 = _reading(VISIT_AT, 44.0 + 1.6, hive_id="PAIR4", side="R")
        r_step2 = _reading(VISIT_AT + timedelta(hours=1), 44.0 + 1.6 + 1.6, hive_id="PAIR4", side="R")
        r_post = _series(VISIT_AT + timedelta(hours=2), 19, 47.2, side="R", hive_id="PAIR4")
        r_readings = r_pre + [r_before, r_step1, r_step2] + r_post

        combined = l_readings + r_readings

        events_by_colony = detect_site_events(combined)
        filtered, quality_by_colony, _meta = filter_quality_issues(combined, events_by_colony)
        features = build_features(filtered, {}, {}, quality_by_colony, events_by_colony=events_by_colony)

        l_feature = next(f for f in features if f.colony_id == "PAIR4:L")
        self.assertGreaterEqual(l_feature.weight_event_count, 1, "L event must survive end-to-end into features")


class TestStaleAnchorNoCascade(unittest.TestCase):
    """T4: targets F4 stale-anchor cascade, event-independent - a single
    genuinely faulty spike reading (excluded correctly) followed by a real,
    noisy-but-legitimate rise. Because the filter's anchor (previous_kept)
    does not advance past the excluded spike, and the fixed absolute jump
    thresholds are not time-normalised, the cumulative gap versus the stale
    anchor keeps re-triggering exclusion for several subsequent, individually
    legitimate readings. Passes now that filter_quality_issues re-admits a run
    of mutually consistent excluded readings (CONSISTENT_RUN_TO_ACCEPT),
    while a lone fault that does not resemble what follows it stays excluded."""

    def test_single_spike_does_not_cascade_into_legitimate_readings(self):
        start = VISIT_AT - timedelta(hours=20)
        # Noisy-but-normal baseline: jitter amplitude large enough that a
        # several-kg move does not clear the MAD z-score threshold (no event),
        # yet the fixed absolute quality-jump thresholds are unaffected by MAD.
        pre = _series(start, 20, 20.0, jitter=1.0, side="L", hive_id="STALE")
        anchor_weight = pre[-1].weight_kg

        # Genuine sensor fault: one reading spikes off the true level. +4.0 kg
        # clears the quality filter's fixed jump thresholds (3.63 kg / 12%)
        # but stays below MAD_SENSITIVITY_K against this colony's own noise
        # (z ~= 4.1 < 5.3), so it is never an event candidate on its own.
        spike_at = start + timedelta(hours=20)
        spike = _reading(spike_at, anchor_weight + 4.0, hive_id="STALE", side="L")

        # Real, continuing rise, clearly displaced from the spike (+8.0 kg,
        # then +1.0 kg/hour): every adjacent pair here -- anchor->spike,
        # spike->rise[0], and each rise[i]->rise[i+1] -- stays under the MAD
        # candidacy threshold, so detect_weight_events sees nothing. But
        # rise[0]'s jump from the spike (4.0 kg, >12%) is well past the
        # quality filter's fixed thresholds, so it (correctly) does not read
        # as a continuation of the spike, and its jump from the *stale*
        # pre-spike anchor keeps the anchor frozen for a few more readings.
        rise = [
            _reading(spike_at + timedelta(hours=i), anchor_weight + 8.0 + 1.0 * (i - 1), hive_id="STALE", side="L")
            for i in range(1, 7)
        ]

        readings = pre + [spike] + rise

        events = detect_weight_events(readings)
        self.assertEqual(events, [], "no genuine event: every adjacent pair stays below MAD_SENSITIVITY_K")

        filtered, _quality_by_colony, _meta = filter_quality_issues(readings)

        filtered_times = {r.observed_at for r in filtered}
        rise_times = {r.observed_at for r in rise}
        excluded_rise_times = rise_times - filtered_times

        self.assertNotIn(spike_at, filtered_times, "the genuine fault must still be excluded")
        self.assertEqual(
            excluded_rise_times, set(), "legitimate post-spike readings must not cascade-exclude"
        )


class TestPRT1LShapeSettleWindow(unittest.TestCase):
    """Step 5 regression test reproducing the PRT_1:L shape: a confirmed
    addition followed by several hours of disturbed internal-temp readings
    (a management visit disturbs the sensor for a while, not just the exact
    event reading). Before Step 5, only the event's exact timestamp was
    exempt, so the temperature swing excluded six consecutive post-event
    readings. EVENT_SETTLE_WINDOW_HOURS now exempts the readings immediately
    after the event, and the anchor it lets through absorbs the rest."""

    def test_confirmed_addition_disturbed_temp_produces_zero_exclusions(self):
        event_at = VISIT_AT
        pre = _series(event_at - timedelta(hours=20), 19, 61.0, side="L", hive_id="PRT")
        before = _reading(event_at - timedelta(hours=1), 61.0, hive_id="PRT", side="L")
        event = _reading(event_at, 68.0, hive_id="PRT", side="L", temp_f=94.0)
        # Six hours of disturbed internal temp following the visit (the
        # PRT_1:L shape), weight flat at the new post-event level.
        disturbed = [
            _reading(event_at + timedelta(hours=i), 68.0 + 0.05 * math.sin(i), hive_id="PRT", side="L", temp_f=60.0)
            for i in range(1, 7)
        ]
        post = _series(event_at + timedelta(hours=7), 14, 68.0, side="L", hive_id="PRT")

        readings = pre + [before, event] + disturbed + post
        events = detect_weight_events(readings)
        self.assertEqual(len(events), 1, "the addition must be confirmed standalone")

        events_by_colony = {"PRT:L": events}
        filtered, _quality_by_colony, meta = filter_quality_issues(readings, events_by_colony)

        filtered_times = {r.observed_at for r in filtered}
        visit_times = {r.observed_at for r in [event] + disturbed}
        self.assertTrue(visit_times.issubset(filtered_times), "no visit or post-visit reading should be excluded")
        self.assertEqual(meta["excluded_sensor_reading_count"], 0)


class TestAggregateMultiStepAddition(unittest.TestCase):
    """T5: targets F5 - a gradual addition split into two sub-floor hourly
    steps is invisible to the per-pair-only detector. Passes now that
    detect_weight_events runs a second AGGREGATE_SPAN_HOURS-span pass."""

    def test_split_addition_detected_as_one_event(self):
        base = VISIT_AT - timedelta(hours=20)
        pre = _series(base, 19, 60.0, side="L", hive_id="SPLIT")
        before = _reading(VISIT_AT - timedelta(hours=1), 60.0, hive_id="SPLIT", side="L")
        # +1.8 kg then +1.8 kg -- each step under MIN_CANDIDATE_ADDITION_KG (3.0).
        step1 = _reading(VISIT_AT, 61.8, hive_id="SPLIT", side="L")
        step2 = _reading(VISIT_AT + timedelta(hours=1), 63.6, hive_id="SPLIT", side="L")
        post = _series(VISIT_AT + timedelta(hours=2), 19, 63.6, side="L", hive_id="SPLIT")

        readings = pre + [before, step1, step2] + post
        events = detect_weight_events(readings)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "addition")

    def test_smooth_nectar_flow_produces_no_events(self):
        # Guard: a sustained, monotonic strong nectar-flow day (+0.3 kg/hour
        # for 10 hours, ~3.0 kg total) must NOT be converted into an
        # "addition" by the aggregate pass. Any 3-hour window within the flow
        # only accumulates ~0.9 kg -- safely under MIN_CANDIDATE_ADDITION_KG
        # -- since AGGREGATE_SPAN_HOURS is fixed, not adaptive to the flow's
        # full duration.
        base = VISIT_AT - timedelta(hours=30)
        pre = _series(base, 20, 40.0, side="L", hive_id="FLOW")
        flow_start = pre[-1].observed_at + timedelta(hours=1)
        flow = [
            _reading(flow_start + timedelta(hours=i), pre[-1].weight_kg + 0.3 * (i + 1), hive_id="FLOW", side="L")
            for i in range(10)
        ]
        post = _series(flow[-1].observed_at + timedelta(hours=1), 20, flow[-1].weight_kg, side="L", hive_id="FLOW")

        readings = pre + flow + post
        events = detect_weight_events(readings)

        self.assertEqual(events, [])


class TestSymmetricGainCorroboration(unittest.TestCase):
    """T6: targets F2 (Step 2 acceptance) - L has a confirmed large addition
    (> 3 kg, z >= MAD_SENSITIVITY_K), R has a same-hour sub-threshold gain
    (z >= MAD_CORROBORATE_K). R must be promoted."""

    def test_confirmed_addition_promotes_sister_sub_threshold_gain(self):
        base = VISIT_AT - timedelta(hours=20)

        # L: confirmed standalone addition (+6 kg on a 45 kg base).
        l_pre = _series(base, 19, 45.0, side="L", hive_id="SITE6")
        l_before = _reading(VISIT_AT - timedelta(hours=1), 45.0, hive_id="SITE6", side="L")
        l_event = _reading(VISIT_AT, 51.0, hive_id="SITE6", side="L")
        l_post = _series(VISIT_AT + timedelta(hours=1), 20, 51.0, side="L", hive_id="SITE6")
        l_readings = l_pre + [l_before, l_event] + l_post

        # R: sub-threshold gain (+2.6 kg on a 44 kg base, ~5.9%). Below the
        # 3.0 kg floor but with a robust-z that clears MAD_CORROBORATE_K.
        r_pre = _series(base, 19, 44.0, side="R", hive_id="SITE6")
        r_before = _reading(VISIT_AT - timedelta(hours=1), 44.0, hive_id="SITE6", side="R")
        r_gain = _reading(VISIT_AT, 46.6, hive_id="SITE6", side="R")
        r_post = _series(VISIT_AT + timedelta(hours=1), 20, 46.6, side="R", hive_id="SITE6")
        r_readings = r_pre + [r_before, r_gain] + r_post

        l_events = detect_weight_events(l_readings)
        r_events = detect_weight_events(r_readings)

        self.assertEqual(len(l_events), 1, "L addition must be confirmed standalone")
        self.assertEqual(r_events, [], "R gain must not fire standalone (below floor)")

        result = corroborate_sister_events(
            {"L": l_events, "R": r_events},
            {"L": l_readings, "R": r_readings},
        )

        self.assertEqual(len(result["L"]), 1, "L event list unchanged")
        self.assertEqual(len(result["R"]), 1, "R gain must be promoted")
        self.assertIn("addition", result["R"][0].kind)
        self.assertGreater(result["R"][0].delta_kg, 0, "Promoted event is a gain")


if __name__ == "__main__":
    unittest.main()
