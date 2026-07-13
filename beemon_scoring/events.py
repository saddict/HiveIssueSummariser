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

# Minimum net delta for a coalesced cluster to be treated as a real event.
# No longer used for single-step candidate detection (replaced by MAD robust-z),
# but still needed by _coalesce_events to drop clusters that net out to nothing
# (e.g. a transient dip that fully recovers within the confirmation window).
MIN_EVENT_DROP_KG = 2.5
MIN_EVENT_DROP_PCT = 7.0

# Physical sanity floors applied before the MAD z-score check.
# Additions must clear a minimum absolute gain: foraging returns rarely exceed
# 2-3 kg in a single hour even on a strong flow day; real supers and feeders
# typically add 3 kg or more. Setting 3.0 filters the foraging-burst false
# positives seen at WTG_HSCHL (peak +2.7 kg) while keeping genuine additions.
MIN_CANDIDATE_ADDITION_KG = 3.0
# Harvests must clear a minimum percentage drop: sensor drift and minor
# calibration shifts are typically < 1-2 %; real harvests remove at least
# 3-4 % of colony weight (6LR:R lost 4.0 % in the 2026-07-07 harvest, the
# smallest confirmed harvest in the dataset).
MIN_CANDIDATE_HARVEST_PCT = 3.0

# MAD-based outlier threshold. A step whose hour-normalised delta scores
# MAD_SENSITIVITY_K or more standard deviations away from the colony's own
# typical inter-reading movement is flagged as a candidate event.
# Value of 5.3 validated via spike_mad_events.py (2026-07-08):
# confirmed harvests reach z ≥ 5.37 (6LR:R); highest ordinary foraging step
# was 4.92 (DR_WLKS:L, active nectar flow). 5.3 gives a 0.38σ buffer above
# that peak while still clearing the confirmed 6LR:R harvest (z = 5.37).
MAD_SENSITIVITY_K = 5.3

# Threshold for sister-corroborated promotion (see corroborate_sister_events in
# features.py). A drop between MAD_CORROBORATE_K and MAD_SENSITIVITY_K on one
# colony is promoted to a confirmed event when its sister at the same site has
# a confirmed event in the same reading window. Value of 4.0 sits below the
# 6LR:R harvest (z=5.47) and above most ordinary foraging noise; the temporal-
# proximity and directional constraints in corroboration prevent false positives
# from the positive foraging spikes seen on DR_WLKS:L (max ordinary z=4.98).
MAD_CORROBORATE_K = 4.0

# Minimum number of usable inter-reading deltas required before MAD-based
# detection is trusted. Below this, the MAD estimate is too noisy.
_MIN_MAD_DELTAS = 8
# MAD below this is treated as a near-flat window where the robust-z diverges;
# skip MAD-based detection for the colony in that case.
_MAD_EPSILON = 1e-6

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


def _robust_step_stats(ordered: list[SensorReading]) -> tuple[float, float, int]:
    """Hour-normalised inter-reading weight deltas, their median and scaled MAD.

    Only pairs within MAX_EVENT_INTERVAL_HOURS with positive weights are used,
    matching the pairs the event detector examines. MAD is scaled by 1.4826 so
    it estimates the standard deviation under a normal distribution.

    Returns (median_delta_per_hour, mad_scaled, n_usable).
    """
    deltas: list[float] = []
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]
        if prev.weight_kg <= 0 or curr.weight_kg <= 0:
            continue
        elapsed_hours = (curr.observed_at - prev.observed_at).total_seconds() / 3600
        if elapsed_hours <= 0 or elapsed_hours > MAX_EVENT_INTERVAL_HOURS:
            continue
        deltas.append((curr.weight_kg - prev.weight_kg) / elapsed_hours)

    n = len(deltas)
    if n < _MIN_MAD_DELTAS:
        return 0.0, 0.0, n

    med = _median(deltas)
    mad = _median([abs(d - med) for d in deltas]) * 1.4826
    return med, mad, n


def detect_weight_events(readings: list[SensorReading]) -> list[WeightEvent]:
    """Return the abrupt weight events in a single colony's readings.

    ``readings`` must belong to one colony. The list is sorted defensively so
    callers do not have to guarantee ordering.
    """
    ordered = sorted(readings, key=lambda reading: reading.timestamp)

    # MAD-based self-calibrating path: characterise typical inter-reading
    # movement for this colony, then flag steps that are statistical outliers.
    median_delta, mad, n_usable = _robust_step_stats(ordered)
    use_mad = n_usable >= _MIN_MAD_DELTAS and mad >= _MAD_EPSILON

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

        # Physical sanity floors before the MAD check. These filter steps that
        # are statistically anomalous but physically implausible as beekeeping
        # events: strong foraging returns look like additions, and sensor drift
        # looks like a tiny harvest. The MAD z-score alone cannot distinguish
        # these because a very stable colony will score even small steps as
        # outliers. Corroboration bypasses these floors intentionally — a
        # sub-threshold drop on the sister side may be physically small yet
        # still represent a real apiary-level event.
        if delta_kg > 0 and delta_kg < MIN_CANDIDATE_ADDITION_KG:
            continue
        if delta_kg < 0 and abs(pct_change) < MIN_CANDIDATE_HARVEST_PCT:
            continue

        # Flag when this step is a clear outlier relative to the colony's own
        # typical hourly movement. If MAD is unavailable (too few readings or
        # near-flat window), emit no candidates for this colony.
        if not use_mad:
            continue
        robust_z = (delta_kg / elapsed_hours - median_delta) / mad
        if abs(robust_z) < MAD_SENSITIVITY_K:
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


def corroborate_sister_events(
    events_by_side: dict[str, list[WeightEvent]],
    readings_by_side: dict[str, list[SensorReading]],
) -> dict[str, list[WeightEvent]]:
    """Promote soft drops on one colony when its sister has a confirmed event.

    Harvests are apiary-level actions: a beekeeper typically works both hives in
    one visit. When one side has a confirmed event (robust-z >= MAD_SENSITIVITY_K)
    and the sister has a sub-threshold drop (MAD_CORROBORATE_K <= z < SENSITIVITY_K)
    within one reading interval of that event, the sister drop is promoted to a
    confirmed event tagged as "sister-corroborated" in its description.

    Constraints:
    - This function is strictly site-level. It never modifies the per-colony
      detector (detect_weight_events) and never calls it; the caller supplies
      already-detected events.
    - Corroboration only promotes DROPS (negative delta_kg) temporally close to
      a confirmed event. Positive steps (additions) are not promoted this way.
    - Single-colony sites skip corroboration silently.
    - Direction and temporal proximity together prevent the foraging-gain spikes
      seen on active colonies (e.g. DR_WLKS:L) from being falsely promoted.
    """
    sides = list(events_by_side.keys())
    if len(sides) < 2:
        return events_by_side

    result: dict[str, list[WeightEvent]] = {side: list(evts) for side, evts in events_by_side.items()}

    for i, confirmed_side in enumerate(sides):
        sister_side = sides[1 - i]
        confirmed_events = events_by_side[confirmed_side]
        sister_readings = sorted(readings_by_side.get(sister_side, []), key=lambda r: r.timestamp)
        if not confirmed_events or not sister_readings:
            continue

        confirmed_times = {e.observed_at for e in confirmed_events}

        # Determine one reading interval for temporal proximity guard.
        intervals: list[float] = []
        for j in range(1, len(sister_readings)):
            h = (sister_readings[j].observed_at - sister_readings[j - 1].observed_at).total_seconds() / 3600
            if 0 < h <= MAX_EVENT_INTERVAL_HOURS:
                intervals.append(h)
        one_interval_hours = _median(intervals) if intervals else 1.0

        # Characterise sister colony's typical movement for robust-z.
        med, mad, n_usable = _robust_step_stats(sister_readings)
        if n_usable < _MIN_MAD_DELTAS or mad < _MAD_EPSILON:
            continue

        # Scan sister readings for sub-threshold drops near a confirmed event.
        already_confirmed = {e.observed_at for e in result[sister_side]}
        for j in range(1, len(sister_readings)):
            prev = sister_readings[j - 1]
            curr = sister_readings[j]
            if curr.observed_at in already_confirmed:
                continue
            if prev.weight_kg <= 0 or curr.weight_kg <= 0:
                continue
            elapsed_h = (curr.observed_at - prev.observed_at).total_seconds() / 3600
            if elapsed_h <= 0 or elapsed_h > MAX_EVENT_INTERVAL_HOURS:
                continue
            delta_kg = curr.weight_kg - prev.weight_kg
            if delta_kg >= 0:
                continue  # only drops can be corroborated as harvests
            robust_z = (delta_kg / elapsed_h - med) / mad
            if not (MAD_CORROBORATE_K <= abs(robust_z) < MAD_SENSITIVITY_K):
                continue

            # Check temporal proximity: within one reading interval of any confirmed event.
            near = any(
                abs((curr.observed_at - ct).total_seconds() / 3600) <= one_interval_hours
                for ct in confirmed_times
            )
            if not near:
                continue

            pct_change = (delta_kg / prev.weight_kg) * 100
            promoted = WeightEvent(
                kind="harvest (sister-corroborated)",
                observed_at=curr.observed_at,
                before_kg=prev.weight_kg,
                after_kg=curr.weight_kg,
                delta_kg=delta_kg,
                pct_change=pct_change,
                elapsed_hours=elapsed_h,
            )
            result[sister_side] = result[sister_side] + [promoted]
            already_confirmed.add(curr.observed_at)

    return result


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
