from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date

from .models import ColonyFeatures, SensorReading, WeatherReading
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
        favorable_daily_changes = daily_weight_pct_changes(readings, day_types, "favorable")
        poor_daily_changes = daily_weight_pct_changes(readings, day_types, "poor")

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
                weight_delta_kg=last.weight_kg - first.weight_kg,
                weight_pct_change=_pct_change(first.weight_kg, last.weight_kg),
                weight_slope_kg_per_day=_linear_slope_per_day(readings),
                weight_slope_pct_per_day=_linear_slope_pct_per_day(readings),
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
            )
        )
    return features


def daily_weight_pct_changes(
    readings: list[SensorReading],
    day_types: dict[date, str],
    weather_type: str,
) -> list[float]:
    by_day: dict[date, list[SensorReading]] = defaultdict(list)
    for reading in readings:
        observed_date = reading.observed_at.date()
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
