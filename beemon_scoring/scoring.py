from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .data_loader import load_hive_config, load_sensor_readings, load_weather_readings
from .features import build_features, stddev
from .metrics import BADNESS_Z_SCORE_SCALE, METRICS, Metric
from .models import ColonyFeatures, ColonyScore, MetricComparison
from .quality import filter_quality_issues
from .weather import weather_by_hive, weather_day_types


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

    _require_data_dir(sensor_dir, "sensor")
    _require_data_dir(weather_dir, "weather")

    sensor_readings = load_sensor_readings(sensor_dir, hives, colony_sides)
    weather_readings = load_weather_readings(weather_dir, hives)

    if not sensor_readings:
        raise RuntimeError("No sensor readings found.")

    window_days = int(window_days or settings["rolling_window_days"])
    end_at = max(reading.observed_at for reading in sensor_readings)
    start_at = end_at - timedelta(days=window_days)

    windowed_sensor_readings = [reading for reading in sensor_readings if reading.observed_at >= start_at]
    filtered_readings, quality_by_colony, quality_summary = filter_quality_issues(windowed_sensor_readings)
    if not filtered_readings:
        raise RuntimeError("No valid sensor readings found after data quality filtering.")

    window_dates = {reading.observed_at.date() for reading in filtered_readings}
    hive_weather = weather_by_hive(weather_readings, window_dates)
    hive_weather_day_types = weather_day_types(hive_weather)

    features = build_features(filtered_readings, hive_weather, hive_weather_day_types, quality_by_colony)
    scores = _score_features(features, settings)
    metadata = {
        "window_days": window_days,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "sensor_reading_count": len(windowed_sensor_readings),
        "valid_sensor_reading_count": len(filtered_readings),
        "excluded_sensor_reading_count": quality_summary["excluded_sensor_reading_count"],
        "data_quality_issue_count": quality_summary["data_quality_issue_count"],
        "weather_reading_count": sum(len(values) for values in hive_weather.values()),
        "colony_count": len(scores),
        "region_count": len({score.region_id for score in scores}),
        "region_ids": sorted({score.region_id for score in scores}),
        "region_assignment_method": "coordinate_radius_connected_components",
        "region_radius_miles": settings["region_radius_miles"],
        "min_colony_days_observed": round(min(score.feature.days_observed for score in scores), 2) if scores else 0,
        "max_colony_days_observed": round(max(score.feature.days_observed for score in scores), 2) if scores else 0,
    }
    return sorted(scores, key=lambda score: (score.region_id, -score.score, score.colony_id)), metadata


def _require_data_dir(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"Missing {label} data directory: {path}")


def _score_features(features: list[ColonyFeatures], settings: dict[str, float]) -> list[ColonyScore]:
    by_region: dict[str, list[ColonyFeatures]] = defaultdict(list)
    for feature in features:
        by_region[feature.region_id].append(feature)

    scores: list[ColonyScore] = []
    for region_id in sorted(by_region):
        region_features = sorted(by_region[region_id], key=lambda feature: feature.colony_id)
        scores.extend(_score_region_features(region_features, settings))
    return scores


def _score_region_features(features: list[ColonyFeatures], settings: dict[str, float]) -> list[ColonyScore]:
    scores: list[ColonyScore] = []
    threshold = settings["zscore_badness_threshold"]
    drop_threshold = settings["weight_drop_pct_threshold"]

    for feature in features:
        comparisons: list[MetricComparison] = []
        raw_score = 0.0
        total_weight = 0.0

        for metric in METRICS:
            eligible_peers = _eligible_metric_peers(features, metric)
            if feature not in eligible_peers or len(eligible_peers) < 2:
                continue
            values = [float(getattr(peer, metric.name)) for peer in eligible_peers]
            peer_mean = statistics.fmean(values)
            peer_std = stddev(values)
            value = float(getattr(feature, metric.name))
            badness_z = _badness_z(value, peer_mean, peer_std, metric.direction)
            metric_weight = metric.weight
            total_weight += metric_weight
            raw_score += max(0.0, badness_z) * metric_weight
            comparisons.append(
                MetricComparison(
                    metric=metric.name,
                    label=metric.label,
                    value=value,
                    peer_mean=peer_mean,
                    peer_std=peer_std,
                    badness_z=badness_z,
                    weight=metric_weight,
                    unit=metric.unit,
                )
            )

        score = round(min(100.0, (raw_score / max(total_weight, 0.001)) * BADNESS_Z_SCORE_SCALE), 1)
        flags = _flags(feature, comparisons, threshold, drop_threshold)
        status = _status(score, flags)
        scores.append(
            ColonyScore(
                colony_id=feature.colony_id,
                region_id=feature.region_id,
                hive_id=feature.hive_id,
                colony_side=feature.colony_side,
                score=score,
                status=status,
                comparisons=sorted(comparisons, key=lambda item: item.badness_z, reverse=True),
                feature=feature,
                flags=flags,
            )
        )

    return sorted(scores, key=lambda score: (-score.score, score.colony_id))


def _eligible_metric_peers(features: list[ColonyFeatures], metric: Metric) -> list[ColonyFeatures]:
    if not metric.min_sample_attr:
        return features
    return [feature for feature in features if getattr(feature, metric.min_sample_attr) >= metric.min_sample_count]


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


