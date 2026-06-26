from __future__ import annotations

from collections import defaultdict

from .events import detect_weight_events
from .models import SensorReading

MIN_WEIGHT_KG = 0.45
MAX_WEIGHT_KG = 136.08
MIN_INTERNAL_TEMP_F = 32.0
MAX_INTERNAL_TEMP_F = 120.0
MIN_EXTERNAL_TEMP_F = -40.0
MAX_EXTERNAL_TEMP_F = 130.0
MIN_HUMIDITY_PCT = 0.0
MAX_HUMIDITY_PCT = 100.0
MAX_WEIGHT_JUMP_PCT = 12.0
MAX_WEIGHT_JUMP_KG = 3.63
MAX_TEMP_JUMP_F = 25.0
MAX_HUMIDITY_JUMP_PCT = 45.0
MAX_JUMP_INTERVAL_HOURS = 6.0


def filter_quality_issues(
    readings: list[SensorReading],
) -> tuple[list[SensorReading], dict[str, list[str]], dict[str, int]]:
    by_colony: dict[str, list[SensorReading]] = defaultdict(list)
    quality_by_colony: dict[str, list[str]] = defaultdict(list)
    filtered: list[SensorReading] = []
    excluded_count = 0

    for reading in readings:
        by_colony[reading.colony_id].append(reading)

    for colony_id, colony_readings in by_colony.items():
        ordered = sorted(colony_readings, key=lambda item: item.timestamp)
        # Detect genuine harvest/swarm/supering steps up front so the jump
        # filter below does not mistake them for sensor faults. A real event
        # produces a sharp, sustained level shift; the detector ignores the
        # transient spikes and dropouts that the jump filter exists to remove.
        # The timestamps of confirmed events mark the readings that open a new
        # baseline and must therefore survive instead of being excluded.
        event_timestamps = {event.observed_at for event in detect_weight_events(ordered)}

        previous_kept: SensorReading | None = None
        for reading in ordered:
            impossible_reasons = _impossible_reading_reasons(reading)
            if impossible_reasons:
                excluded_count += 1
                quality_by_colony[colony_id].append(
                    f"Excluded reading at {reading.observed_at.isoformat()} because {', '.join(impossible_reasons)}."
                )
                continue

            for reason in _external_sensor_reasons(reading):
                quality_by_colony[colony_id].append(
                    f"External sensor anomaly at {reading.observed_at.isoformat()}: {reason}."
                )

            is_event_step = reading.observed_at in event_timestamps
            if previous_kept is not None and not is_event_step:
                jump_reasons = _sudden_jump_reasons(previous_kept, reading)
                if jump_reasons:
                    excluded_count += 1
                    quality_by_colony[colony_id].append(
                        f"Excluded reading at {reading.observed_at.isoformat()} because {', '.join(jump_reasons)}."
                    )
                    continue

            filtered.append(reading)
            previous_kept = reading

    issue_count = sum(len(values) for values in quality_by_colony.values())
    return sorted(filtered, key=lambda reading: (reading.hive_id, reading.colony_side, reading.timestamp)), quality_by_colony, {
        "excluded_sensor_reading_count": excluded_count,
        "data_quality_issue_count": issue_count,
    }


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
