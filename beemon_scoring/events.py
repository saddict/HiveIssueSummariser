"""Detection of abrupt hive-weight events (harvests, swarms, supering/feeding).

The rest of the pipeline treats colony weight as a single smooth trend over the
rolling window: it fits one first-vs-last delta and one straight line through
every reading. That assumption breaks whenever a beekeeper harvests honey, a
colony swarms, or a super/feeder is added, because each of those shifts the
baseline by several kilograms in an hour or two. A single slope drawn across
that step either reports a thriving colony as collapsing (harvest) or a
declining one as booming (supering), and the event itself is never surfaced.

This module finds those step discontinuities and splits the window into
"segments" -- the stretches of normal day-to-day behaviour between events. The
feature builder then scores each segment on its own and combines them, so the
trend reflects how the colony actually behaves rather than the size of the
beekeeper's intervention. The events themselves are reported as explicit flags
instead of being silently averaged away or dropped as sensor noise.

The detector is deliberately conservative. Ordinary foraging gains, nightly
respiration loss, and sensor jitter stay within a single segment; only a
sustained level shift that survives a short confirmation window counts as an
event. That keeps a normal week as one segment (identical to today's
behaviour) while isolating the genuine harvest/swarm/addition cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import SensorReading

# A level shift only counts when it clears BOTH an absolute and a relative
# floor. The absolute floor stops tiny colonies from registering an "event"
# every time a few hundred grams move; the relative floor stops large colonies
# from hiding a real harvest inside normal-looking kilograms.
MIN_EVENT_DROP_KG = 2.5
MIN_EVENT_DROP_PCT = 7.0

# How quickly the shift has to happen. A harvest or a swarm departure moves the
# scale within an hour or two; seasonal nectar flow does not. Anything slower
# than this stays inside a single segment and is scored as an ordinary trend.
MAX_EVENT_INTERVAL_HOURS = 8.0

# After a candidate step we look a short way ahead and require the new level to
# persist. This rejects one-off spikes and dropouts (a single 0.0 reading, a
# transient overload) that revert immediately, while accepting a true baseline
# change that holds.
CONFIRMATION_WINDOW_HOURS = 12.0
# Fraction of the original step that must still be present at the end of the
# confirmation window for the shift to be treated as a real, persistent event.
CONFIRMATION_RETENTION = 0.5

# Segments shorter than this are not scored on their own -- there is not enough
# post-event data yet to fit a meaningful trend, so the short tail is folded
# into the neighbouring segment for trend purposes (the event is still flagged).
MIN_SEGMENT_HOURS = 12.0


@dataclass(frozen=True)
class WeightEvent:
    """A single abrupt change in colony weight within the window."""

    kind: str  # "harvest", "swarm", "addition"
    observed_at: datetime
    before_kg: float
    after_kg: float
    delta_kg: float
    pct_change: float
    elapsed_hours: float

    def describe(self) -> str:
        direction = "gained" if self.delta_kg > 0 else "dropped"
        return (
            f"Likely {self.kind}: weight {direction} {abs(self.delta_kg):.1f} kg "
            f"({self.pct_change:+.1f}%) around {self.observed_at.isoformat()}."
        )


@dataclass(frozen=True)
class WeightSegment:
    """A contiguous run of readings between events, scored as one trend."""

    readings: list[SensorReading]

    @property
    def start_at(self) -> datetime:
        return self.readings[0].observed_at

    @property
    def end_at(self) -> datetime:
        return self.readings[-1].observed_at

    @property
    def hours(self) -> float:
        return (self.end_at - self.start_at).total_seconds() / 3600


def detect_weight_events(readings: list[SensorReading]) -> list[WeightEvent]:
    """Return the abrupt weight events in a single colony's readings.

    ``readings`` must belong to one colony. The list is sorted defensively so
    callers do not have to guarantee ordering.
    """
    ordered = sorted(readings, key=lambda reading: reading.timestamp)
    candidates: list[WeightEvent] = []

    for index in range(1, len(ordered)):
        previous = ordered[index - 1]
        current = ordered[index]
        elapsed_hours = (current.observed_at - previous.observed_at).total_seconds() / 3600
        if elapsed_hours <= 0 or elapsed_hours > MAX_EVENT_INTERVAL_HOURS:
            continue
        if previous.weight_kg <= 0 or current.weight_kg <= 0:
            # A 0.0 reading is a sensor dropout, not a real level. Skip the
            # pair; the surrounding readings will still be compared once the
            # dropout clears.
            continue

        delta_kg = current.weight_kg - previous.weight_kg
        pct_change = (delta_kg / previous.weight_kg) * 100
        if abs(delta_kg) < MIN_EVENT_DROP_KG or abs(pct_change) < MIN_EVENT_DROP_PCT:
            continue

        candidates.append(
            WeightEvent(
                kind=_classify(previous, current, delta_kg),
                observed_at=current.observed_at,
                before_kg=previous.weight_kg,
                after_kg=current.weight_kg,
                delta_kg=delta_kg,
                pct_change=pct_change,
                elapsed_hours=elapsed_hours,
            )
        )

    # Coalesce clustered steps (drop-then-settle, dip-then-recover) into single
    # net events FIRST, then confirm each merged event actually persists. Doing
    # it in this order means a transient dip and its recovery cancel out before
    # either half can be mistaken for a standalone event.
    merged = _coalesce_events(candidates)
    confirmed: list[WeightEvent] = []
    timestamp_index = {reading.observed_at: position for position, reading in enumerate(ordered)}
    for event in merged:
        position = timestamp_index.get(event.observed_at)
        if position is None:
            continue
        if _shift_persists(ordered, position, event.before_kg, event.delta_kg):
            confirmed.append(event)
    return confirmed


def _coalesce_events(events: list[WeightEvent]) -> list[WeightEvent]:
    """Merge events that cluster together into a single net event.

    A scale rarely lands on its new value in one clean step. A harvest often
    reads as a sharp drop followed an hour later by a partial settle back up;
    a brief overload reads as a spike then a return. Treated as separate events
    these create a sliver segment around the disturbance and, worse, leave the
    bottom-of-the-drop reading stranded at the tail of the previous segment --
    which silently pulls the step back into the trend the segmentation was
    meant to remove.

    Events within CONFIRMATION_WINDOW_HOURS of one another are collapsed into
    one event running from the cluster's opening ``before_kg`` to its closing
    ``after_kg``. If the coalesced net move falls back under the detection
    floors (a dip that fully recovers), it is dropped entirely -- nothing
    really happened.
    """
    if not events:
        return events

    clusters: list[list[WeightEvent]] = [[events[0]]]
    for event in events[1:]:
        last = clusters[-1][-1]
        gap_hours = (event.observed_at - last.observed_at).total_seconds() / 3600
        if gap_hours <= CONFIRMATION_WINDOW_HOURS:
            clusters[-1].append(event)
        else:
            clusters.append([event])

    coalesced: list[WeightEvent] = []
    for cluster in clusters:
        if len(cluster) == 1:
            coalesced.append(cluster[0])
            continue

        first = cluster[0]
        last = cluster[-1]
        net_delta = last.after_kg - first.before_kg
        net_pct = (net_delta / first.before_kg) * 100 if first.before_kg else 0.0
        if abs(net_delta) < MIN_EVENT_DROP_KG or abs(net_pct) < MIN_EVENT_DROP_PCT:
            # The cluster nets out to nothing meaningful -- a transient that
            # recovered. Drop it so it neither flags nor splits the window.
            continue

        # Anchor the merged event on the largest single step in the cluster so
        # the timestamp (segment boundary) lands on the real discontinuity, and
        # so the temperature/humidity signature used for classification comes
        # from the dominant move rather than the settle.
        dominant = max(cluster, key=lambda item: abs(item.delta_kg))
        kind = "addition" if net_delta > 0 else dominant.kind
        if net_delta < 0 and dominant.delta_kg > 0:
            kind = "harvest"
        coalesced.append(
            WeightEvent(
                kind=kind,
                observed_at=dominant.observed_at,
                before_kg=first.before_kg,
                after_kg=last.after_kg,
                delta_kg=net_delta,
                pct_change=net_pct,
                elapsed_hours=(last.observed_at - first.observed_at).total_seconds() / 3600,
            )
        )

    return coalesced


def segment_readings(
    readings: list[SensorReading], events: list[WeightEvent]
) -> list[WeightSegment]:
    """Split readings into segments separated by the detected events.

    Each event's ``observed_at`` opens a new segment: the reading at that
    timestamp is the first reading after the step, so it begins the post-event
    segment. Segments shorter than ``MIN_SEGMENT_HOURS`` are merged into the
    previous segment so trend fitting always has enough data to work with.
    """
    ordered = sorted(readings, key=lambda reading: reading.timestamp)
    if not events:
        return [WeightSegment(readings=ordered)]

    boundaries = sorted(event.observed_at for event in events)
    segments: list[list[SensorReading]] = [[]]
    boundary_index = 0
    for reading in ordered:
        while boundary_index < len(boundaries) and reading.observed_at >= boundaries[boundary_index]:
            segments.append([])
            boundary_index += 1
        segments[-1].append(reading)

    built = [WeightSegment(readings=group) for group in segments if group]
    return _merge_short_segments(built)


def _shift_persists(
    ordered: list[SensorReading], index: int, baseline_kg: float, delta_kg: float
) -> bool:
    """Check the new level still holds CONFIRMATION_RETENTION of the step.

    Looks ahead up to CONFIRMATION_WINDOW_HOURS and compares the median of the
    post-step readings against the pre-step baseline. A transient spike or a
    lone dropout reverts and fails this test; a genuine harvest or supering
    holds and passes.
    """
    step_time = ordered[index].observed_at
    lookahead: list[float] = []
    for reading in ordered[index:]:
        if (reading.observed_at - step_time).total_seconds() / 3600 > CONFIRMATION_WINDOW_HOURS:
            break
        if reading.weight_kg > 0:
            lookahead.append(reading.weight_kg)

    if not lookahead:
        return False

    sustained_level = _median(lookahead)
    sustained_delta = sustained_level - baseline_kg
    # The sustained move has to be in the same direction as the step and retain
    # at least CONFIRMATION_RETENTION of its magnitude.
    if delta_kg == 0:
        return False
    if (sustained_delta > 0) != (delta_kg > 0):
        return False
    return abs(sustained_delta) >= abs(delta_kg) * CONFIRMATION_RETENTION


def _classify(previous: SensorReading, current: SensorReading, delta_kg: float) -> str:
    """Label the event from its weight direction and thermal signature.

    Weight alone cannot distinguish a harvest from a swarm -- both are sudden
    drops. A swarm removes a large mass of bees, which tends to disturb the
    brood-nest temperature and humidity far more than lifting honey supers does,
    so a drop paired with a sharp internal-climate change leans "swarm".
    Upward steps are management actions (adding a super, a feeder, or a box):
    "addition". The labels are explicitly probabilistic -- describe()/reporting
    always says "Likely ..." -- because the sensors cannot confirm the cause.
    """
    if delta_kg > 0:
        return "addition"

    temp_swing = abs(current.internal_temp_f - previous.internal_temp_f)
    humidity_swing = abs(current.internal_humidity_pct - previous.internal_humidity_pct)
    if temp_swing >= 5.0 or humidity_swing >= 10.0:
        return "swarm"
    return "harvest"


def _merge_short_segments(segments: list[WeightSegment]) -> list[WeightSegment]:
    if len(segments) <= 1:
        return segments

    merged: list[list[SensorReading]] = [list(segments[0].readings)]
    for segment in segments[1:]:
        if segment.hours < MIN_SEGMENT_HOURS:
            merged[-1].extend(segment.readings)
        else:
            merged.append(list(segment.readings))
    return [WeightSegment(readings=group) for group in merged]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
