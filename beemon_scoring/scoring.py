from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from .data_loader import load_hive_config, load_sensor_readings, load_weather_readings
from .models import ColonyFeatures, ColonyScore, MetricComparison, SensorReading, WeatherReading

BROOD_TARGET_TEMP_F = 94.5
HIGH_HUMIDITY_PCT = 70.0
LOW_HUMIDITY_PCT = 40.0
RAINY_WEATHER_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}

MIN_WEIGHT_LB = 1.0
MAX_WEIGHT_LB = 300.0
MIN_INTERNAL_TEMP_F = 32.0
MAX_INTERNAL_TEMP_F = 120.0
MIN_EXTERNAL_TEMP_F = -40.0
MAX_EXTERNAL_TEMP_F = 130.0
MIN_HUMIDITY_PCT = 0.0
MAX_HUMIDITY_PCT = 100.0
MAX_WEIGHT_JUMP_PCT = 12.0
MAX_WEIGHT_JUMP_LB = 8.0
MAX_TEMP_JUMP_F = 25.0
MAX_HUMIDITY_JUMP_PCT = 45.0
MAX_JUMP_INTERVAL_HOURS = 6.0

METRICS = [
    {
        "name": "weight_pct_change",
        "label": "7-day weight percent change",
        "direction": "higher_is_better",
        "weight": 0.26,
        "unit": "%",
    },
    {
        "name": "weight_slope_pct_per_day",
        "label": "weight percent trend",
        "direction": "higher_is_better",
        "weight": 0.16,
        "unit": "%/day",
    },
    {
        "name": "favorable_weather_weight_slope_pct_per_day",
        "label": "favorable-weather weight percent trend",
        "direction": "higher_is_better",
        "weight": 0.10,
        "unit": "%/day",
    },
    {
        "name": "poor_weather_weight_loss_pct",
        "label": "poor-weather weight loss",
        "direction": "lower_is_better",
        "weight": 0.06,
        "unit": "%",
    },
    {
        "name": "internal_temp_std_f",
        "label": "temperature instability",
        "direction": "lower_is_better",
        "weight": 0.15,
        "unit": "F",
    },
    {
        "name": "avg_brood_temp_deviation_f",
        "label": "possible brood-temperature variation",
        "direction": "lower_is_better",
        "weight": 0.13,
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
    filtered_readings, quality_by_colony, quality_summary = _filter_quality_issues(windowed_sensor_readings)
    if not filtered_readings:
        raise RuntimeError("No valid sensor readings found after data quality filtering.")

    window_dates = {reading.observed_at.date() for reading in filtered_readings}
    weather_by_hive = _weather_by_hive(weather_readings, window_dates)
    weather_day_types = _weather_day_types(weather_by_hive)

    features = _build_features(filtered_readings, weather_by_hive, weather_day_types, quality_by_colony)
    scores = _score_features(features, settings)
    metadata = {
        "window_days": window_days,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "sensor_reading_count": len(windowed_sensor_readings),
        "valid_sensor_reading_count": len(filtered_readings),
        "excluded_sensor_reading_count": quality_summary["excluded_sensor_reading_count"],
        "data_quality_issue_count": quality_summary["data_quality_issue_count"],
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


def _filter_quality_issues(
    readings: list[SensorReading],
) -> tuple[list[SensorReading], dict[str, list[str]], dict[str, int]]:
    by_colony: dict[str, list[SensorReading]] = defaultdict(list)
    quality_by_colony: dict[str, list[str]] = defaultdict(list)
    filtered: list[SensorReading] = []
    excluded_count = 0

    for reading in readings:
        by_colony[reading.colony_id].append(reading)

    for colony_id, colony_readings in by_colony.items():
        previous_kept: SensorReading | None = None
        for reading in sorted(colony_readings, key=lambda item: item.timestamp):
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

            if previous_kept is not None:
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
    if not MIN_WEIGHT_LB <= reading.weight_lb <= MAX_WEIGHT_LB:
        reasons.append(f"weight {reading.weight_lb:.2f} lb is outside {MIN_WEIGHT_LB:.0f}-{MAX_WEIGHT_LB:.0f} lb")
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
    weight_delta = abs(current.weight_lb - previous.weight_lb)
    weight_delta_pct = (weight_delta / previous.weight_lb) * 100 if previous.weight_lb else 0
    temp_delta = abs(current.internal_temp_f - previous.internal_temp_f)
    humidity_delta = abs(current.internal_humidity_pct - previous.internal_humidity_pct)

    if weight_delta > MAX_WEIGHT_JUMP_LB and weight_delta_pct > MAX_WEIGHT_JUMP_PCT:
        reasons.append(f"weight jumped {weight_delta:.2f} lb ({weight_delta_pct:.1f}%) in {elapsed_hours:.1f} hours")
    if temp_delta > MAX_TEMP_JUMP_F:
        reasons.append(f"internal temperature jumped {temp_delta:.1f} F in {elapsed_hours:.1f} hours")
    if humidity_delta > MAX_HUMIDITY_JUMP_PCT:
        reasons.append(f"internal humidity jumped {humidity_delta:.1f}% in {elapsed_hours:.1f} hours")
    return reasons


def _weather_day_types(weather_by_hive: dict[str, list[WeatherReading]]) -> dict[str, dict[date, str]]:
    by_hive_day: dict[str, dict[date, list[WeatherReading]]] = defaultdict(lambda: defaultdict(list))
    for hive_id, readings in weather_by_hive.items():
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

    poor = rainy or (avg_temp is not None and (avg_temp < 50 or avg_temp > 95)) or (avg_cloud is not None and avg_cloud >= 85)
    favorable = (
        not rainy
        and avg_temp is not None
        and 55 <= avg_temp <= 90
        and (avg_cloud is None or avg_cloud < 75)
        and (avg_humidity is None or avg_humidity < 90)
    )
    if poor:
        return "poor"
    if favorable:
        return "favorable"
    return "neutral"


def _build_features(
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
        weights = [reading.weight_lb for reading in readings]
        temps = [reading.internal_temp_f for reading in readings]
        humidities = [reading.internal_humidity_pct for reading in readings]
        external_temps = [reading.external_temp_f for reading in readings if reading.external_temp_f is not None]
        external_humidities = [reading.external_humidity_pct for reading in readings if reading.external_humidity_pct is not None]
        weather = weather_by_hive.get(first.hive_id, [])
        day_types = weather_day_types.get(first.hive_id, {})
        favorable_readings = [reading for reading in readings if day_types.get(reading.observed_at.date()) == "favorable"]
        poor_readings = [reading for reading in readings if day_types.get(reading.observed_at.date()) == "poor"]
        poor_pct_change = _pct_change_for_readings(poor_readings)

        features.append(
            ColonyFeatures(
                colony_id=colony_id,
                hive_id=first.hive_id,
                colony_side=first.colony_side,
                sample_count=len(readings),
                excluded_reading_count=_excluded_count(quality_by_colony.get(colony_id, [])),
                data_quality_flags=quality_by_colony.get(colony_id, [])[:8],
                start_at=first.observed_at,
                end_at=last.observed_at,
                days_observed=elapsed_days,
                latest_weight_lb=last.weight_lb,
                weight_delta_lb=last.weight_lb - first.weight_lb,
                weight_pct_change=_pct_change(first.weight_lb, last.weight_lb),
                weight_slope_lb_per_day=_linear_slope_per_day(readings),
                weight_slope_pct_per_day=_linear_slope_pct_per_day(readings),
                favorable_weather_sample_count=len(favorable_readings),
                poor_weather_sample_count=len(poor_readings),
                favorable_weather_weight_slope_pct_per_day=_linear_slope_pct_per_day(favorable_readings)
                if len(favorable_readings) >= 2
                else _linear_slope_pct_per_day(readings),
                poor_weather_weight_loss_pct=abs(min(0.0, poor_pct_change)) if poor_pct_change is not None else 0.0,
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
    if feature.excluded_reading_count:
        flags.append(f"Excluded {feature.excluded_reading_count} sensor readings due to data quality checks.")
    for data_flag in feature.data_quality_flags[:2]:
        flags.append(f"Data quality: {data_flag}")
    return flags


def _status(score: float, flags: list[str]) -> str:
    quality_flags = [flag for flag in flags if flag.startswith("Data quality:") or flag.startswith("Excluded ")]
    performance_flags = [flag for flag in flags if flag not in quality_flags]
    if score >= 55 or len(performance_flags) >= 3:
        return "underperforming"
    if score >= 30 or performance_flags or quality_flags:
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


def _linear_slope_pct_per_day(readings: list[SensorReading]) -> float:
    if not readings or readings[0].weight_lb == 0:
        return 0.0
    return (_linear_slope_per_day(readings) / readings[0].weight_lb) * 100


def _pct_change_for_readings(readings: list[SensorReading]) -> float | None:
    if len(readings) < 2:
        return None
    return _pct_change(readings[0].weight_lb, readings[-1].weight_lb)


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


def _average_optional(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return statistics.fmean(clean)


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
