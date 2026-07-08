from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date

from .events import WeightSegment, corroborate_sister_events, detect_weight_events, segment_readings
from .models import ColonyFeatures, SensorReading, WeatherReading
from .thermal import thermal_efficiency
from .weather import RAINY_WEATHER_CODES

BROOD_TARGET_TEMP_F = 94.5
HIGH_HUMIDITY_PCT = 70.0
LOW_HUMIDITY_PCT = 40.0


def build_features(
    sensor_readings: list[SensorReading],
    weather_by_hive: dict[str, list[WeatherReading]],
    weather_day_types: dict[str, dict[date, str]],
    quality_by_colony: dict[str, list[str]],
) -> list[ColonyFeatures]:
    by_colony: dict[str, list[SensorReading]] = defaultdict(list)
    for reading in sensor_readings:
        by_colony[reading.colony_id].append(reading)

    # Run per-colony event detection, then apply site-level sister corroboration
    # so a soft drop on one side can be promoted when the sister has a confirmed
    # event in the same reading window (harvests are apiary-level actions).
    by_hive: dict[str, dict[str, list[SensorReading]]] = defaultdict(lambda: defaultdict(list))
    for reading in sensor_readings:
        by_hive[reading.hive_id][reading.colony_side].append(reading)

    events_by_colony: dict[str, list] = {}
    for hive_id, sides in by_hive.items():
        raw_events = {side: detect_weight_events(rdgs) for side, rdgs in sides.items()}
        corroborated = corroborate_sister_events(raw_events, sides)
        for side, evts in corroborated.items():
            events_by_colony[f"{hive_id}:{side}"] = evts

    features: list[ColonyFeatures] = []
    for colony_id, readings in sorted(by_colony.items()):
        readings = sorted(readings, key=lambda reading: reading.timestamp)
        first = readings[0]
        last = readings[-1]
        elapsed_days = max((last.observed_at - first.observed_at).total_seconds() / 86400, 1 / 24)
        temps = [reading.internal_temp_f for reading in readings]
        humidities = [reading.internal_humidity_pct for reading in readings]
        external_temps = [reading.external_temp_f for reading in readings if reading.external_temp_f is not None]
        external_humidities = [reading.external_humidity_pct for reading in readings if reading.external_humidity_pct is not None]
        weather = weather_by_hive.get(first.hive_id, [])
        day_types = weather_day_types.get(first.hive_id, {})

        # Use pre-computed events (with sister corroboration applied at site level).
        events = events_by_colony.get(colony_id, [])
        segments = segment_readings(readings, events)
        weight_trend = _segmented_weight_trend(segments)
        event_dates = {event.observed_at.date() for event in events}

        favorable_daily_changes = daily_weight_pct_changes(readings, day_types, "favorable", event_dates)
        poor_daily_changes = daily_weight_pct_changes(readings, day_types, "poor", event_dates)

        te_result = thermal_efficiency(readings)
        te_pi = te_result["Pi"] if te_result else 0.0
        te_m = te_result["m"] if te_result else 0.0
        te_count = te_result["n"] if te_result else 0

        features.append(
            ColonyFeatures(
                colony_id=colony_id,
                region_id=first.region_id,
                hive_id=first.hive_id,
                colony_side=first.colony_side,
                sample_count=len(readings),
                excluded_reading_count=_excluded_count(quality_by_colony.get(colony_id, [])),
                data_quality_flags=quality_by_colony.get(colony_id, [])[:8],
                start_at=first.observed_at,
                end_at=last.observed_at,
                days_observed=elapsed_days,
                latest_weight_kg=last.weight_kg,
                weight_delta_kg=weight_trend["delta_kg"],
                weight_pct_change=weight_trend["pct_change"],
                weight_slope_kg_per_day=weight_trend["slope_kg_per_day"],
                weight_slope_pct_per_day=weight_trend["slope_pct_per_day"],
                favorable_weather_window_count=len(favorable_daily_changes),
                poor_weather_window_count=len(poor_daily_changes),
                favorable_weather_weight_slope_pct_per_day=statistics.fmean(favorable_daily_changes)
                if favorable_daily_changes
                else 0.0,
                poor_weather_weight_loss_pct=statistics.fmean(abs(min(0.0, change)) for change in poor_daily_changes)
                if poor_daily_changes
                else 0.0,
                avg_internal_temp_f=statistics.fmean(temps),
                internal_temp_std_f=stddev(temps),
                avg_brood_temp_deviation_f=statistics.fmean(abs(value - BROOD_TARGET_TEMP_F) for value in temps),
                avg_internal_humidity_pct=statistics.fmean(humidities),
                internal_humidity_std_pct=stddev(humidities),
                high_humidity_reading_pct=_share_pct(value > HIGH_HUMIDITY_PCT for value in humidities),
                low_humidity_reading_pct=_share_pct(value < LOW_HUMIDITY_PCT for value in humidities),
                avg_external_temp_f=statistics.fmean(external_temps) if external_temps else None,
                avg_external_humidity_pct=statistics.fmean(external_humidities) if external_humidities else None,
                avg_weather_temp_f=_weather_average(weather, "temperature_f"),
                avg_weather_humidity_pct=_weather_average(weather, "humidity_pct"),
                rainy_weather_reading_pct=_share_pct(
                    reading.weather_code in RAINY_WEATHER_CODES for reading in weather if reading.weather_code is not None
                )
                if weather
                else None,
                cloudy_weather_reading_pct=_share_pct(
                    reading.cloudiness_pct is not None and reading.cloudiness_pct >= 70 for reading in weather
                )
                if weather
                else None,
                dominant_weather_overview=_dominant_weather_overview(weather),
                weight_event_count=len(events),
                weight_event_descriptions=[event.describe() for event in events],
                segment_count=len(segments),
                thermal_efficiency_pi=te_pi,
                thermal_efficiency_m=te_m,
                thermal_paired_count=te_count,
            )
        )
    return features


def _segmented_weight_trend(segments: list[WeightSegment]) -> dict[str, float]:
    """Combine per-segment weight trends into window-level features.

    The window is split at each detected event (harvest, swarm, supering), so a
    segment only ever contains ordinary day-to-day movement. Two quantities are
    rebuilt from the segments:

    * Net weight change (delta_kg / pct_change) is the SUM of each segment's
      own first-to-last change. Because the segment boundaries fall on the
      events, the artificial step jumps are excluded from the total -- what
      remains is the colony's organic gain or loss. pct_change is expressed
      against the very first reading so it stays comparable to the old metric.

    * Trend (slope_kg_per_day / slope_pct_per_day) is a span-weighted average of
      each segment's own linear slope. A long stable segment counts for more
      than a short one, and a harvest no longer drags the slope down to a false
      "collapsing" reading.

    With no events there is exactly one segment, so every value reduces to the
    original first-vs-last / single-regression behaviour and nothing changes for
    a normal week.
    """
    scored = [segment for segment in segments if len(segment.readings) >= 2]
    if not scored:
        return {"delta_kg": 0.0, "pct_change": 0.0, "slope_kg_per_day": 0.0, "slope_pct_per_day": 0.0}

    baseline_kg = scored[0].readings[0].weight_kg
    total_delta_kg = sum(
        segment.readings[-1].weight_kg - segment.readings[0].weight_kg for segment in scored
    )

    slope_weight = 0.0
    slope_kg_accumulator = 0.0
    slope_pct_accumulator = 0.0
    for segment in scored:
        span_days = max(segment.hours / 24, 1 / 24)
        slope_weight += span_days
        slope_kg_accumulator += _linear_slope_per_day(segment.readings) * span_days
        slope_pct_accumulator += _linear_slope_pct_per_day(segment.readings) * span_days

    slope_kg_per_day = slope_kg_accumulator / slope_weight if slope_weight else 0.0
    slope_pct_per_day = slope_pct_accumulator / slope_weight if slope_weight else 0.0

    return {
        "delta_kg": total_delta_kg,
        "pct_change": _pct_change(baseline_kg, baseline_kg + total_delta_kg),
        "slope_kg_per_day": slope_kg_per_day,
        "slope_pct_per_day": slope_pct_per_day,
    }


def daily_weight_pct_changes(
    readings: list[SensorReading],
    day_types: dict[date, str],
    weather_type: str,
    event_dates: set[date] | None = None,
) -> list[float]:
    # Skip any day on which a harvest/swarm/supering occurred: the intraday
    # first-vs-last change on such a day reflects the beekeeper's intervention,
    # not the weather's effect on the colony, and would otherwise blow up the
    # poor-/favorable-weather metrics exactly the way it blew up the overall
    # trend. Days without events are unaffected.
    skip_dates = event_dates or set()
    by_day: dict[date, list[SensorReading]] = defaultdict(list)
    for reading in readings:
        observed_date = reading.observed_at.date()
        if observed_date in skip_dates:
            continue
        if day_types.get(observed_date) == weather_type:
            by_day[observed_date].append(reading)

    changes: list[float] = []
    for day_readings in by_day.values():
        ordered = sorted(day_readings, key=lambda reading: reading.timestamp)
        if len(ordered) >= 2:
            changes.append(_pct_change(ordered[0].weight_kg, ordered[-1].weight_kg))
    return changes


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


def _linear_slope_per_day(readings: list[SensorReading]) -> float:
    if len(readings) < 2:
        return 0.0
    start = readings[0].observed_at
    xs = [(reading.observed_at - start).total_seconds() / 86400 for reading in readings]
    ys = [reading.weight_kg for reading in readings]
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _linear_slope_pct_per_day(readings: list[SensorReading]) -> float:
    if not readings or readings[0].weight_kg == 0:
        return 0.0
    return (_linear_slope_per_day(readings) / readings[0].weight_kg) * 100


def _pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return ((end - start) / start) * 100


def _share_pct(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return sum(1 for value in values if value) / len(values) * 100


def _weather_average(weather: list[WeatherReading], attr: str) -> float | None:
    values = [getattr(reading, attr) for reading in weather if getattr(reading, attr) is not None]
    if not values:
        return None
    return statistics.fmean(values)


def _dominant_weather_overview(weather: list[WeatherReading]) -> str | None:
    overviews = [reading.overview for reading in weather if reading.overview]
    if not overviews:
        return None
    return Counter(overviews).most_common(1)[0][0]


def _excluded_count(flags: list[str]) -> int:
    return sum(1 for flag in flags if flag.startswith("Excluded reading"))
