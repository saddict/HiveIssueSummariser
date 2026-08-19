from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from .events import WeightEvent, detect_weight_events
from .models import SensorReading

MIN_WEIGHT_KG = 0.45
MAX_WEIGHT_KG = 136.08
MIN_INTERNAL_TEMP_F = 32.0
MAX_INTERNAL_TEMP_F = 120.0
MIN_EXTERNAL_TEMP_F = -40.0
MAX_EXTERNAL_TEMP_F = 130.0
MIN_HUMIDITY_PCT = 0.0
MAX_HUMIDITY_PCT = 100.0
# Sudden-jump thresholds: a short-interval move larger than these is treated as a
# sensor artifact and the reading is excluded. Grounded against the real
# inter-reading movement distribution via spike_quality_thresholds.py (2026-07-27,
# full raw history, 11,400 normal pairs with event-adjacent pairs excluded).
# Decision rule: keep a value if it sits >= p99.9 of normal movement so it clips
# only genuine outliers; genuine harvests/supering are exempt anyway (detected
# first). All four thresholds clear that bar with margin, so none were nudged:
#   weight kg  3.63 -> 99.96th pct (p99.9 = 1.23 kg, max normal 5.99 kg)
#   weight %   12.0 -> 99.98th pct (p99.9 = 4.05 %)   [applied jointly with kg]
#   temp F     25.0 -> 99.95th pct (p99.9 = 11.3 F)
#   humidity   45.0 -> 99.99th pct (p99.9 = 18.4 pp)
MAX_WEIGHT_JUMP_PCT = 12.0
MAX_WEIGHT_JUMP_KG = 3.63
MAX_TEMP_JUMP_F = 25.0
MAX_HUMIDITY_JUMP_PCT = 45.0
MAX_JUMP_INTERVAL_HOURS = 6.0

# A management visit disturbs the sensors (temperature, humidity, sometimes
# weight) for a while, not just for the single reading at the event's exact
# timestamp. Readings within this window AFTER a confirmed event are exempt
# from the jump checks (all three types); impossible-range checks still apply.
EVENT_SETTLE_WINDOW_HOURS = 3.0

# If this many consecutive readings are each excluded as a "jump" versus the
# stale previous_kept anchor, but are mutually consistent with one another
# (each one a small step from the last), treat them as a real sustained level
# shift the detector missed rather than sensor noise, and re-admit the run.
CONSISTENT_RUN_TO_ACCEPT = 3


def filter_quality_issues(
    readings: list[SensorReading],
    events_by_colony: dict[str, list[WeightEvent]] | None = None,
) -> tuple[list[SensorReading], dict[str, list[str]], dict[str, int]]:
    by_colony: dict[str, list[SensorReading]] = defaultdict(list)
    quality_by_colony: dict[str, list[str]] = defaultdict(list)
    filtered: list[SensorReading] = []
    excluded_count = 0

    for reading in readings:
        by_colony[reading.colony_id].append(reading)

    for colony_id, colony_readings in by_colony.items():
        ordered = sorted(colony_readings, key=lambda item: item.timestamp)
        # Genuine harvest/swarm/supering/paired/corroborated steps are exempt
        # from the jump filter below so it does not mistake them for sensor
        # faults. A real event produces a sharp, sustained level shift; the
        # detector ignores the transient spikes and dropouts that the jump
        # filter exists to remove. The timestamps of confirmed events mark the
        # readings that open a new baseline and must therefore survive instead
        # of being excluded.
        #
        # When the caller supplies events_by_colony (the single-source-of-
        # truth path via detect_site_events), those events -- including ones
        # only visible after sister corroboration -- are used directly and no
        # detection is run here. Falling back to self-detection (events_by_colony
        # is None) preserves the old per-colony-only behaviour for direct
        # callers that have not adopted the site-level pipeline.
        if events_by_colony is not None:
            colony_events = events_by_colony.get(colony_id, [])
        else:
            colony_events = detect_weight_events(ordered)
        event_times = sorted(event.observed_at for event in colony_events)

        previous_kept: SensorReading | None = None
        # Readings tentatively excluded as a "jump" versus previous_kept, held
        # here in case they turn out to be the start of a sustained shift the
        # detector missed (see CONSISTENT_RUN_TO_ACCEPT).
        pending: list[tuple[SensorReading, list[str]]] = []

        def flush_pending_as_excluded() -> None:
            nonlocal excluded_count
            for pending_reading, reasons in pending:
                excluded_count += 1
                quality_by_colony[colony_id].append(
                    f"Excluded reading at {pending_reading.observed_at.isoformat()} because {', '.join(reasons)}."
                )
            pending.clear()

        for reading in ordered:
            impossible_reasons = _impossible_reading_reasons(reading)
            if impossible_reasons:
                # A genuinely impossible value can never anchor a sustained
                # shift; it also breaks any run in progress.
                flush_pending_as_excluded()
                excluded_count += 1
                quality_by_colony[colony_id].append(
                    f"Excluded reading at {reading.observed_at.isoformat()} because {', '.join(impossible_reasons)}."
                )
                continue

            for reason in _external_sensor_reasons(reading):
                quality_by_colony[colony_id].append(
                    f"External sensor anomaly at {reading.observed_at.isoformat()}: {reason}."
                )

            in_settle_window = any(
                event_time <= reading.observed_at <= event_time + timedelta(hours=EVENT_SETTLE_WINDOW_HOURS)
                for event_time in event_times
            )
            if previous_kept is None or in_settle_window:
                flush_pending_as_excluded()
                filtered.append(reading)
                previous_kept = reading
                continue

            jump_reasons = _sudden_jump_reasons(previous_kept, reading)
            if not jump_reasons:
                flush_pending_as_excluded()
                filtered.append(reading)
                previous_kept = reading
                continue

            # Excluded versus the (possibly stale) anchor. Check whether it is
            # consistent with the run building up so far -- a real sustained
            # shift moves in small, mutually consistent steps; an isolated
            # sensor fault does not resemble what comes after it.
            if pending and _sudden_jump_reasons(pending[-1][0], reading):
                flush_pending_as_excluded()
            pending.append((reading, jump_reasons))

            if len(pending) < CONSISTENT_RUN_TO_ACCEPT:
                continue

            shift_kg = abs(pending[-1][0].weight_kg - previous_kept.weight_kg)
            quality_by_colony[colony_id].append(
                f"Possible missed event: sustained level shift of {shift_kg:.2f} kg near "
                f"{pending[0][0].observed_at.isoformat()} accepted after {len(pending)} consistent readings."
            )
            for pending_reading, _reasons in pending:
                filtered.append(pending_reading)
            previous_kept = pending[-1][0]
            pending.clear()

        flush_pending_as_excluded()

    issue_count = sum(len(values) for values in quality_by_colony.values())
    return sorted(filtered, key=lambda reading: (reading.hive_id, reading.colony_side, reading.timestamp)), quality_by_colony, {
        "excluded_sensor_reading_count": excluded_count,
        "data_quality_issue_count": issue_count,
    }


_FLAG_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}")


def issue_dates(flags: list[str]) -> set[date]:
    """Calendar dates touched by the quality flags emitted above. Every flag
    embeds the ISO timestamp of the reading it describes, so how long a fault
    lasted can be recovered without changing the flag payload the reports print.
    Used by scoring to separate a one-off glitch from a persistent sensor fault.
    """
    dates: set[date] = set()
    for flag in flags:
        for match in _FLAG_DATE_PATTERN.finditer(flag):
            dates.add(date.fromisoformat(match.group(1)))
    return dates


def _impossible_reading_reasons(reading: SensorReading) -> list[str]:
    reasons: list[str] = []
    if not MIN_WEIGHT_KG <= reading.weight_kg <= MAX_WEIGHT_KG:
        reasons.append(f"weight {reading.weight_kg:.2f} kg is outside {MIN_WEIGHT_KG:.2f}-{MAX_WEIGHT_KG:.2f} kg")
    if not MIN_INTERNAL_TEMP_F <= reading.internal_temp_f <= MAX_INTERNAL_TEMP_F:
        reasons.append(
            f"internal temperature {reading.internal_temp_f:.1f} F is outside {MIN_INTERNAL_TEMP_F:.0f}-{MAX_INTERNAL_TEMP_F:.0f} F"
        )
    if not MIN_HUMIDITY_PCT <= reading.internal_humidity_pct <= MAX_HUMIDITY_PCT:
        reasons.append(f"internal humidity {reading.internal_humidity_pct:.1f}% is outside 0-100%")
    return reasons


def _external_sensor_reasons(reading: SensorReading) -> list[str]:
    reasons: list[str] = []
    if reading.external_temp_f is not None and not MIN_EXTERNAL_TEMP_F <= reading.external_temp_f <= MAX_EXTERNAL_TEMP_F:
        reasons.append(
            f"external temperature {reading.external_temp_f:.1f} F is outside {MIN_EXTERNAL_TEMP_F:.0f}-{MAX_EXTERNAL_TEMP_F:.0f} F"
        )
    if reading.external_humidity_pct is not None and not MIN_HUMIDITY_PCT <= reading.external_humidity_pct <= MAX_HUMIDITY_PCT:
        reasons.append(f"external humidity {reading.external_humidity_pct:.1f}% is outside 0-100%")
    return reasons


def _sudden_jump_reasons(previous: SensorReading, current: SensorReading) -> list[str]:
    elapsed_hours = (current.observed_at - previous.observed_at).total_seconds() / 3600
    if elapsed_hours <= 0 or elapsed_hours > MAX_JUMP_INTERVAL_HOURS:
        return []

    reasons: list[str] = []
    weight_delta = abs(current.weight_kg - previous.weight_kg)
    weight_delta_pct = (weight_delta / previous.weight_kg) * 100 if previous.weight_kg else 0
    temp_delta = abs(current.internal_temp_f - previous.internal_temp_f)
    humidity_delta = abs(current.internal_humidity_pct - previous.internal_humidity_pct)

    if weight_delta > MAX_WEIGHT_JUMP_KG and weight_delta_pct > MAX_WEIGHT_JUMP_PCT:
        reasons.append(f"weight jumped {weight_delta:.2f} kg ({weight_delta_pct:.1f}%) in {elapsed_hours:.1f} hours")
    if temp_delta > MAX_TEMP_JUMP_F:
        reasons.append(f"internal temperature jumped {temp_delta:.1f} F in {elapsed_hours:.1f} hours")
    if humidity_delta > MAX_HUMIDITY_JUMP_PCT:
        reasons.append(f"internal humidity jumped {humidity_delta:.1f}% in {elapsed_hours:.1f} hours")
    return reasons
