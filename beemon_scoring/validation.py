"""Validate detected weight events against a beekeeper ground-truth log.

The event detector's precision/recall is only meaningful against events a
beekeeper actually confirms (harvests, supering, feeding, observed swarms) plus
known non-events (foraging bursts that must NOT be flagged). This module loads
that ground truth and matches it against detector output, producing precision,
recall, F1, and a confusion matrix.

Two honesty rules keep the metrics defensible against a partial log:

  * ``label: "non_event"`` records are explicit assertions that no event of the
    given direction exists near a timestamp (e.g. a foraging burst).
  * false positives are only counted inside declared ``audited_ranges`` — spans
    (per site/colony) the beekeeper has fully accounted for. A detection outside
    every audited range is reported separately as "unaudited", never as a false
    positive, so an incomplete log cannot manufacture false alarms.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class GroundTruthRecord:
    site_id: str
    colony_side: str
    observed_at: datetime
    label: str  # "event" or "non_event"
    kinds: list[str]  # acceptable base kinds, e.g. ["harvest"] or ["harvest", "swarm"]
    approx_delta_kg: float | None = None
    source: str = ""
    notes: str = ""

    @property
    def colony_id(self) -> str:
        return f"{self.site_id}:{self.colony_side}"


@dataclass(frozen=True)
class AuditedRange:
    site_id: str
    colony_side: str  # a specific side, or "*" for both
    start: datetime
    end: datetime

    def contains(self, colony_id: str, observed_at: datetime) -> bool:
        site, side = colony_id.split(":")
        if site != self.site_id:
            return False
        if self.colony_side != "*" and side != self.colony_side:
            return False
        return self.start <= observed_at < self.end


@dataclass
class MatchResult:
    true_positives: list[dict] = field(default_factory=list)
    false_negatives: list[dict] = field(default_factory=list)
    false_positives: list[dict] = field(default_factory=list)
    true_negatives: list[dict] = field(default_factory=list)
    unaudited_detections: list[dict] = field(default_factory=list)

    @property
    def precision(self) -> float:
        tp, fp = len(self.true_positives), len(self.false_positives)
        return tp / (tp + fp) if (tp + fp) else 1.0

    @property
    def recall(self) -> float:
        tp, fn = len(self.true_positives), len(self.false_negatives)
        return tp / (tp + fn) if (tp + fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def confusion(self) -> dict[str, int]:
        return {
            "true_positive": len(self.true_positives),
            "false_negative": len(self.false_negatives),
            "false_positive": len(self.false_positives),
            "true_negative": len(self.true_negatives),
            "unaudited_detection": len(self.unaudited_detections),
        }

    def tp_offset_hours(self) -> list[float]:
        return [entry["offset_hours"] for entry in self.true_positives]


def base_kind(kind: str) -> str:
    """Return the base event kind ("harvest", "addition", "swarm").

    Corroboration is now carried on the event's ``corroborated`` flag rather
    than baked into the kind string, so this is a plain normaliser; the split
    is retained defensively in case a kind ever carries a trailing qualifier.
    """
    return kind.split(" ")[0]


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_ground_truth(path: Path) -> tuple[list[GroundTruthRecord], list[AuditedRange]]:
    payload = json.loads(Path(path).read_text())
    records = [
        GroundTruthRecord(
            site_id=r["site_id"],
            colony_side=r["colony_side"],
            observed_at=_parse_dt(r["observed_at"]),
            label=r["label"],
            kinds=list(r["kinds"]),
            approx_delta_kg=r.get("approx_delta_kg"),
            source=r.get("source", ""),
            notes=r.get("notes", ""),
        )
        for r in payload.get("records", [])
    ]
    audited = [
        AuditedRange(
            site_id=a["site_id"],
            colony_side=a.get("colony_side", "*"),
            start=_parse_dt(a["start"]),
            end=_parse_dt(a["end"]),
        )
        for a in payload.get("audited_ranges", [])
    ]
    return records, audited


def _in_any_audited_range(colony_id: str, observed_at: datetime, audited: list[AuditedRange]) -> bool:
    return any(a.contains(colony_id, observed_at) for a in audited)


def match_events(
    detected_by_colony: dict[str, list],
    records: list[GroundTruthRecord],
    audited_ranges: list[AuditedRange],
    tolerance_hours: float = 6.0,
) -> MatchResult:
    """Match detections to ground truth, one detection to at most one record.

    ``detected_by_colony`` maps ``"SITE:SIDE"`` to a list of WeightEvent-like
    objects with ``.kind``, ``.observed_at``, and ``.delta_kg``.
    """
    result = MatchResult()
    tolerance = timedelta(hours=tolerance_hours)

    # Work on a mutable copy so matched detections can be consumed.
    remaining: dict[str, list] = {cid: list(events) for cid, events in detected_by_colony.items()}

    event_records = [r for r in records if r.label == "event"]
    non_event_records = [r for r in records if r.label == "non_event"]

    # Event records: greedy nearest-first, one detection per record.
    for record in event_records:
        pool = remaining.get(record.colony_id, [])
        candidates = [
            ev
            for ev in pool
            if base_kind(ev.kind) in record.kinds and abs(ev.observed_at - record.observed_at) <= tolerance
        ]
        if not candidates:
            result.false_negatives.append(_record_entry(record))
            continue
        best = min(candidates, key=lambda ev: abs(ev.observed_at - record.observed_at))
        pool.remove(best)
        result.true_positives.append(
            {
                **_record_entry(record),
                "detected_kind": best.kind,
                "detected_at": best.observed_at.isoformat(),
                "detected_delta_kg": round(best.delta_kg, 3),
                "offset_hours": round((best.observed_at - record.observed_at).total_seconds() / 3600, 2),
            }
        )

    # Non-event records: any surviving detection of the declared kind nearby is a
    # false alarm; otherwise a true negative.
    for record in non_event_records:
        pool = remaining.get(record.colony_id, [])
        violators = [
            ev
            for ev in pool
            if base_kind(ev.kind) in record.kinds and abs(ev.observed_at - record.observed_at) <= tolerance
        ]
        if violators:
            offender = min(violators, key=lambda ev: abs(ev.observed_at - record.observed_at))
            pool.remove(offender)
            result.false_positives.append(
                {
                    **_record_entry(record),
                    "detected_kind": offender.kind,
                    "detected_at": offender.observed_at.isoformat(),
                    "reason": "detection at a declared non-event",
                }
            )
        else:
            result.true_negatives.append(_record_entry(record))

    # Any detection still unmatched: a false positive if it falls inside an
    # audited range, otherwise reported as unaudited (excluded from precision).
    for colony_id, pool in remaining.items():
        for ev in pool:
            entry = {
                "colony_id": colony_id,
                "detected_kind": ev.kind,
                "detected_at": ev.observed_at.isoformat(),
                "detected_delta_kg": round(ev.delta_kg, 3),
            }
            if _in_any_audited_range(colony_id, ev.observed_at, audited_ranges):
                entry["reason"] = "detection in audited range with no matching record"
                result.false_positives.append(entry)
            else:
                result.unaudited_detections.append(entry)

    return result


def _record_entry(record: GroundTruthRecord) -> dict:
    return {
        "colony_id": record.colony_id,
        "observed_at": record.observed_at.isoformat(),
        "kinds": record.kinds,
        "source": record.source,
        "notes": record.notes,
    }
