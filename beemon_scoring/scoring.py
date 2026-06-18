from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .data_loader import load_hive_config, load_sensor_readings, load_weather_readings
from .models import ColonyFeatures, ColonyScore, MetricComparison, SensorReading, WeatherReading

BROOD_TARGET_TEMP_F = 94.5
HIGH_HUMIDITY_PCT = 70.0
LOW_HUMIDITY_PCT = 40.0
RAINY_WEATHER_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}

METRICS = [
    {
        "name": "weight_delta_lb",
        "label": "7-day weight change",
        "direction": "higher_is_better",
        "weight": 0.34,
        "unit": "lb",
    },
    {
        "name": "weight_slope_lb_per_day",
        "label": "weight trend",
        "direction": "higher_is_better",
        "weight": 0.22,
        "unit": "lb/day",
    },
    {
        "name": "internal_temp_std_f",
        "label": "temperature instability",
        "direction": "lower_is_better",
        "weight": 0.16,
        "unit": "F",
    },
    {
        "name": "avg_brood_temp_deviation_f",
        "label": "brood-temperature deviation",
        "direction": "lower_is_better",
        "weight": 0.14,
        "unit": "F",
    },
    {
        "name": "high_humidity_reading_pct",
        "label": "high-humidity exposure",
        "direction": "lower_is_better",
        "weight": 0.08,
        "unit": "%",
    },
    {
        "name": "internal_humidity_std_pct",
        "label": "humidity instability",
        "direction": "lower_is_better",
        "weight": 0.06,
        "unit": "%",
    },
]


def build_scores(
    project_root: Path,
    window_days: int | None = None,
    sensor_dir: Path | None = None,
    weather_dir: Path | None = None,
) -> tuple[list[ColonyScore], dict[str, object]]:
    hives, colony_sides, settings = load_hive_config(project_root / "hive_config.py")
    default_local_data = project_root / "local_data"
    sensor_dir = sensor_dir or default_local_data / "dynamodb"
    weather_dir = weather_dir or default_local_data / "openmeteo"

    if not sensor_dir.exists():
        sensor_dir = project_root / "Data"
    if not weather_dir.exists():
        weather_dir = project_root / "Data"

    sensor_readings = load_sensor_readings(sensor_dir, hives, colony_sides)
    weather_readings = load_weather_readings(weather_dir, hives)

    if not sensor_readings:
        raise RuntimeError("No sensor readings found.")

    window_days = int(window_days or settings["rolling_window_days"])
    end_at = max(reading.observed_at for reading in sensor_readings)
    start_at = end_at - timedelta(days=window_days)

    windowed_sensor_readings = [reading for reading in sensor_readings if reading.observed_at >= start_at]
    window_dates = {reading.observed_at.date() for reading in windowed_sensor_readings}
    weather_by_hive = _weather_by_hive(weather_readings, window_dates)

    features = _build_features(windowed_sensor_readings, weather_by_hive)
    scores = _score_features(features, settings)
    metadata = {
        "window_days": window_days,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "sensor_reading_count": len(windowed_sensor_readings),
        "weather_reading_count": sum(len(values) for values in weather_by_hive.values()),
        "colony_count": len(scores),
        "min_colony_days_observed": round(min(score.feature.days_observed for score in scores), 2) if scores else 0,
        "max_colony_days_observed": round(max(score.feature.days_observed for score in scores), 2) if scores else 0,
    }
    return sorted(scores, key=lambda score: score.score, reverse=True), metadata


def _weather_by_hive(
    weather_readings: list[WeatherReading],
    allowed_dates: set[date],
) -> dict[str, list[WeatherReading]]:
    grouped: dict[str, list[WeatherReading]] = defaultdict(list)
    for reading in weather_readings:
        if not allowed_dates or reading.observed_date in allowed_dates:
            grouped[reading.hive_id].append(reading)
    return grouped


def _build_features(
    sensor_readings: list[SensorReading],
    weather_by_hive: dict[str, list[WeatherReading]],
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
        weights = [reading.weight_lb for reading in readings]
        temps = [reading.internal_temp_f for reading in readings]
        humidities = [reading.internal_humidity_pct for reading in readings]
        external_temps = [reading.external_temp_f for reading in readings if reading.external_temp_f is not None]
        external_humidities = [reading.external_humidity_pct for reading in readings if reading.external_humidity_pct is not None]
        weather = weather_by_hive.get(first.hive_id, [])

        features.append(
            ColonyFeatures(
                colony_id=colony_id,
                hive_id=first.hive_id,
                colony_side=first.colony_side,
                sample_count=len(readings),
                start_at=first.observed_at,
                end_at=last.observed_at,
                days_observed=elapsed_days,
                latest_weight_lb=last.weight_lb,
                weight_delta_lb=last.weight_lb - first.weight_lb,
                weight_pct_change=_pct_change(first.weight_lb, last.weight_lb),
                weight_slope_lb_per_day=_linear_slope_per_day(readings),
                avg_internal_temp_f=statistics.fmean(temps),
                internal_temp_std_f=_stddev(temps),
                avg_brood_temp_deviation_f=statistics.fmean(abs(value - BROOD_TARGET_TEMP_F) for value in temps),
                avg_internal_humidity_pct=statistics.fmean(humidities),
                internal_humidity_std_pct=_stddev(humidities),
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


def _score_features(features: list[ColonyFeatures], settings: dict[str, float]) -> list[ColonyScore]:
    scores: list[ColonyScore] = []
    threshold = settings["zscore_badness_threshold"]
    drop_threshold = settings["weight_drop_pct_threshold"]

    for feature in features:
        comparisons: list[MetricComparison] = []
        raw_score = 0.0
        total_weight = 0.0

        for metric in METRICS:
            values = [float(getattr(peer, metric["name"])) for peer in features]
            peer_mean = statistics.fmean(values)
            peer_std = _stddev(values)
            value = float(getattr(feature, metric["name"]))
            badness_z = _badness_z(value, peer_mean, peer_std, metric["direction"])
            metric_weight = float(metric["weight"])
            total_weight += metric_weight
            raw_score += max(0.0, badness_z) * metric_weight
            comparisons.append(
                MetricComparison(
                    metric=metric["name"],
                    label=metric["label"],
                    value=value,
                    peer_mean=peer_mean,
                    peer_std=peer_std,
                    badness_z=badness_z,
                    weight=metric_weight,
                    unit=metric["unit"],
                )
            )

        score = round(min(100.0, (raw_score / max(total_weight, 0.001)) * 35), 1)
        flags = _flags(feature, comparisons, threshold, drop_threshold)
        status = _status(score, flags)
        scores.append(
            ColonyScore(
                colony_id=feature.colony_id,
                hive_id=feature.hive_id,
                colony_side=feature.colony_side,
                score=score,
                status=status,
                comparisons=sorted(comparisons, key=lambda item: item.badness_z, reverse=True),
                feature=feature,
                flags=flags,
            )
        )

    return scores


def _badness_z(value: float, peer_mean: float, peer_std: float, direction: str) -> float:
    if peer_std < 0.000001:
        return 0.0
    z = (value - peer_mean) / peer_std
    if direction == "higher_is_better":
        z *= -1
    return z


def _flags(
    feature: ColonyFeatures,
    comparisons: list[MetricComparison],
    threshold: float,
    drop_threshold: float,
) -> list[str]:
    flags: list[str] = []
    if feature.weight_pct_change <= -drop_threshold:
        flags.append(f"Weight dropped {abs(feature.weight_pct_change):.1f}% during the window.")
    for comparison in comparisons:
        if comparison.badness_z >= threshold:
            flags.append(
                f"{comparison.label} is {comparison.badness_z:.1f} standard deviations worse than peers."
            )
    return flags


def _status(score: float, flags: list[str]) -> str:
    if score >= 55 or len(flags) >= 3:
        return "underperforming"
    if score >= 30 or flags:
        return "watch"
    return "normal"


def _linear_slope_per_day(readings: list[SensorReading]) -> float:
    if len(readings) < 2:
        return 0.0
    start = readings[0].observed_at
    xs = [(reading.observed_at - start).total_seconds() / 86400 for reading in readings]
    ys = [reading.weight_lb for reading in readings]
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return ((end - start) / start) * 100


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


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

