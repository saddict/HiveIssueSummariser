from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from .models import WeatherReading

RAINY_WEATHER_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}

POOR_WEATHER_LOW_TEMP_F = 50.0
POOR_WEATHER_HIGH_TEMP_F = 95.0
POOR_WEATHER_CLOUDINESS_PCT = 85.0
FAVORABLE_WEATHER_LOW_TEMP_F = 55.0
FAVORABLE_WEATHER_HIGH_TEMP_F = 90.0
FAVORABLE_WEATHER_CLOUDINESS_PCT = 75.0
FAVORABLE_WEATHER_HUMIDITY_PCT = 90.0


def weather_by_hive(
    weather_readings: list[WeatherReading],
    allowed_dates: set[date],
) -> dict[str, list[WeatherReading]]:
    grouped: dict[str, list[WeatherReading]] = defaultdict(list)
    for reading in weather_readings:
        if not allowed_dates or reading.observed_date in allowed_dates:
            grouped[reading.hive_id].append(reading)
    return grouped


def weather_day_types(hive_weather: dict[str, list[WeatherReading]]) -> dict[str, dict[date, str]]:
    by_hive_day: dict[str, dict[date, list[WeatherReading]]] = defaultdict(lambda: defaultdict(list))
    for hive_id, readings in hive_weather.items():
        for reading in readings:
            by_hive_day[hive_id][reading.observed_date].append(reading)

    day_types: dict[str, dict[date, str]] = defaultdict(dict)
    for hive_id, by_day in by_hive_day.items():
        for observed_date, readings in by_day.items():
            day_types[hive_id][observed_date] = _classify_weather_day(readings)
    return day_types


def _classify_weather_day(readings: list[WeatherReading]) -> str:
    rainy = any(reading.weather_code in RAINY_WEATHER_CODES for reading in readings if reading.weather_code is not None)
    avg_temp = _average_optional([reading.temperature_f for reading in readings])
    avg_cloud = _average_optional([reading.cloudiness_pct for reading in readings])
    avg_humidity = _average_optional([reading.humidity_pct for reading in readings])

    poor = (
        rainy
        or (avg_temp is not None and (avg_temp < POOR_WEATHER_LOW_TEMP_F or avg_temp > POOR_WEATHER_HIGH_TEMP_F))
        or (avg_cloud is not None and avg_cloud >= POOR_WEATHER_CLOUDINESS_PCT)
    )
    favorable = (
        not rainy
        and avg_temp is not None
        and FAVORABLE_WEATHER_LOW_TEMP_F <= avg_temp <= FAVORABLE_WEATHER_HIGH_TEMP_F
        and (avg_cloud is None or avg_cloud < FAVORABLE_WEATHER_CLOUDINESS_PCT)
        and (avg_humidity is None or avg_humidity < FAVORABLE_WEATHER_HUMIDITY_PCT)
    )
    if poor:
        return "poor"
    if favorable:
        return "favorable"
    return "neutral"


def _average_optional(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return statistics.fmean(clean)
