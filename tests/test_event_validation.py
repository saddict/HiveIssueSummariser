"""Unit tests for the event-detection validation matcher (validation.py)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from beemon_scoring.events import WeightEvent
from beemon_scoring.validation import (
    AuditedRange,
    GroundTruthRecord,
    base_kind,
    match_events,
)

TZ = ZoneInfo("America/New_York")
T0 = datetime(2026, 7, 7, 17, 0, tzinfo=TZ)


def event(kind: str, at: datetime, delta_kg: float = -3.0) -> WeightEvent:
    return WeightEvent(
        kind=kind,
        observed_at=at,
        before_kg=50.0,
        after_kg=50.0 + delta_kg,
        delta_kg=delta_kg,
        pct_change=delta_kg / 50.0 * 100,
        elapsed_hours=1.0,
    )


def record(kinds, at=T0, label="event", side="L", site="6LR") -> GroundTruthRecord:
    return GroundTruthRecord(
        site_id=site, colony_side=side, observed_at=at, label=label, kinds=list(kinds)
    )


def audited(site="6LR", side="L", around=T0) -> AuditedRange:
    return AuditedRange(site_id=site, colony_side=side, start=around - timedelta(hours=12), end=around + timedelta(hours=12))


class BaseKindTests(unittest.TestCase):
    def test_normalises_qualified_kinds(self) -> None:
        # Corroboration is carried on the event's flag now, not in the kind
        # string, so kinds are already bare; base_kind stays defensive against
        # any trailing qualifier a kind might ever carry.
        self.assertEqual(base_kind("harvest"), "harvest")
        self.assertEqual(base_kind("addition"), "addition")
        self.assertEqual(base_kind("harvest (qualified)"), "harvest")


class MatchEventsTests(unittest.TestCase):
    def test_exact_true_positive(self) -> None:
        result = match_events({"6LR:L": [event("harvest", T0)]}, [record(["harvest"])], [audited()])
        self.assertEqual(result.confusion()["true_positive"], 1)
        self.assertEqual(result.precision, 1.0)
        self.assertEqual(result.recall, 1.0)

    def test_true_positive_at_tolerance_boundary(self) -> None:
        near = T0 + timedelta(hours=6)
        result = match_events({"6LR:L": [event("harvest", near)]}, [record(["harvest"])], [audited()], tolerance_hours=6.0)
        self.assertEqual(result.confusion()["true_positive"], 1)

    def test_miss_outside_tolerance_is_fn_and_fp_in_range(self) -> None:
        far = T0 + timedelta(hours=9)
        result = match_events({"6LR:L": [event("harvest", far)]}, [record(["harvest"])], [audited()], tolerance_hours=6.0)
        self.assertEqual(result.confusion()["false_negative"], 1)
        # the unmatched detection falls inside the audited range -> false positive
        self.assertEqual(result.confusion()["false_positive"], 1)

    def test_kind_mismatch_not_matched(self) -> None:
        result = match_events({"6LR:L": [event("addition", T0, delta_kg=4.0)]}, [record(["harvest"])], [audited()])
        self.assertEqual(result.confusion()["false_negative"], 1)
        self.assertEqual(result.confusion()["false_positive"], 1)

    def test_qualified_kind_matches_base(self) -> None:
        result = match_events({"6LR:L": [event("harvest (qualified)", T0)]}, [record(["harvest"])], [audited()])
        self.assertEqual(result.confusion()["true_positive"], 1)

    def test_ambiguous_record_matches_swarm(self) -> None:
        result = match_events({"6LR:L": [event("swarm", T0)]}, [record(["harvest", "swarm"])], [audited()])
        self.assertEqual(result.confusion()["true_positive"], 1)

    def test_non_event_violation_is_false_positive(self) -> None:
        result = match_events(
            {"WTG_HSCHL:R": [event("addition", T0, delta_kg=4.0)]},
            [record(["addition"], label="non_event", site="WTG_HSCHL", side="R")],
            [],
        )
        self.assertEqual(result.confusion()["false_positive"], 1)
        self.assertEqual(result.confusion()["true_negative"], 0)

    def test_non_event_clean_is_true_negative(self) -> None:
        result = match_events(
            {"WTG_HSCHL:R": []},
            [record(["addition"], label="non_event", site="WTG_HSCHL", side="R")],
            [],
        )
        self.assertEqual(result.confusion()["true_negative"], 1)
        self.assertEqual(result.confusion()["false_positive"], 0)

    def test_detection_outside_audited_range_is_unaudited_not_fp(self) -> None:
        # No records, no audited ranges: a stray detection is unaudited, not a FP.
        result = match_events({"6LR:L": [event("harvest", T0)]}, [], [])
        self.assertEqual(result.confusion()["false_positive"], 0)
        self.assertEqual(result.confusion()["unaudited_detection"], 1)
        self.assertEqual(result.precision, 1.0)

    def test_greedy_one_to_one_two_records_one_detection(self) -> None:
        r1 = record(["harvest"], at=T0)
        r2 = record(["harvest"], at=T0 + timedelta(hours=1))
        result = match_events({"6LR:L": [event("harvest", T0)]}, [r1, r2], [audited()])
        self.assertEqual(result.confusion()["true_positive"], 1)
        self.assertEqual(result.confusion()["false_negative"], 1)


if __name__ == "__main__":
    unittest.main()
