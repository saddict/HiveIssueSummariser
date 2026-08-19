from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from .data_loader import load_hive_config, load_sensor_readings, load_weather_readings
from .events import detect_site_events
from .features import build_features, stddev
from .metrics import BADNESS_Z_SCORE_SCALE, METRICS, Metric
from .models import ColonyFeatures, ColonyScore, HiveConfig, MetricComparison, SensorReading
from .quality import filter_quality_issues
from .thresholds import integer, number
from .weather import weather_by_hive, weather_day_types

# Status cuts and the peer-pool floor; see thresholds.toml [status], [scoring].
UNDERPERFORMING_SCORE = number("status.underperforming_score")
WATCH_SCORE = number("status.watch_score")
UNDERPERFORMING_FLAG_COUNT = integer("status.underperforming_flag_count")
MIN_PEER_COUNT = integer("scoring.min_peer_count")


def prepare_features(
    project_root: Path,
    window_days: int | None = None,
    sensor_dir: Path | None = None,
    weather_dir: Path | None = None,
) -> tuple[list[ColonyFeatures], dict[str, float], dict[str, object]]:
    """Load, window, event-detect, quality-filter, and feature-build — the whole
    score-independent half of the pipeline. Returns ``(features, settings,
    prep_metadata)``. Split out so callers that rescore under many metric-weight
    variants (e.g. spike_weight_sensitivity.py) prepare the data once and rescore
    cheaply, instead of re-running the entire pipeline per variant."""
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
    # Report back the window actually used, not the configured default: scoring
    # measures data-quality coverage against it.
    settings = {**settings, "rolling_window_days": float(window_days)}
    end_at = max(reading.observed_at for reading in sensor_readings)
    start_at = end_at - timedelta(days=window_days)

    windowed_sensor_readings = [reading for reading in sensor_readings if reading.observed_at >= start_at]
    # Detect + corroborate events once, up front, on the pre-filter readings.
    # The quality filter and feature builder both consume this same result so
    # a corroborated event (only visible after sister corroboration) is
    # exempt from data-quality checks instead of being invisible to a second,
    # independent detection pass over a different slice of the data.
    events_by_colony = detect_site_events(windowed_sensor_readings)
    filtered_readings, quality_by_colony, quality_summary = filter_quality_issues(
        windowed_sensor_readings, events_by_colony
    )
    if not filtered_readings:
        raise RuntimeError("No valid sensor readings found after data quality filtering.")

    window_dates = {reading.observed_at.date() for reading in filtered_readings}
    hive_weather = weather_by_hive(weather_readings, window_dates)
    hive_weather_day_types = weather_day_types(hive_weather)

    features = build_features(
        filtered_readings, hive_weather, hive_weather_day_types, quality_by_colony, events_by_colony=events_by_colony
    )
    # Measured over the FULL history, not the window: a colony that fell silent
    # before the window opens has no windowed readings to be late in.
    not_reporting = _reporting_gaps(
        sensor_readings, hives, colony_sides, end_at, settings["max_reporting_gap_days"]
    )
    prep_metadata = {
        "not_reporting": not_reporting,
        "window_days": window_days,
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "sensor_reading_count": len(windowed_sensor_readings),
        "valid_sensor_reading_count": len(filtered_readings),
        "excluded_sensor_reading_count": quality_summary["excluded_sensor_reading_count"],
        "data_quality_issue_count": quality_summary["data_quality_issue_count"],
        "weather_reading_count": sum(len(values) for values in hive_weather.values()),
        "region_radius_miles": settings["region_radius_miles"],
        "min_region_site_count": settings["min_region_site_count"],
    }
    return features, settings, prep_metadata


def build_scores(
    project_root: Path,
    window_days: int | None = None,
    sensor_dir: Path | None = None,
    weather_dir: Path | None = None,
) -> tuple[list[ColonyScore], dict[str, object]]:
    features, settings, prep = prepare_features(project_root, window_days, sensor_dir, weather_dir)
    scores = _score_features(features, settings)
    not_reporting = cast(list[dict], prep["not_reporting"])
    scores = _apply_reporting_gaps(
        scores, not_reporting, cast(str, prep["start_at"]), cast(str, prep["end_at"])
    )
    reporting = [score for score in scores if score.feature.sample_count]
    metadata = {
        "window_days": prep["window_days"],
        "start_at": prep["start_at"],
        "end_at": prep["end_at"],
        "sensor_reading_count": prep["sensor_reading_count"],
        "valid_sensor_reading_count": prep["valid_sensor_reading_count"],
        "excluded_sensor_reading_count": prep["excluded_sensor_reading_count"],
        "data_quality_issue_count": prep["data_quality_issue_count"],
        "weather_reading_count": prep["weather_reading_count"],
        # Counts describe the colonies actually compared; colonies that are not
        # reporting are listed separately so they never inflate coverage stats.
        "colony_count": len(reporting),
        "region_count": len({score.region_id for score in scores}),
        "region_ids": sorted({score.region_id for score in scores}),
        "region_assignment_method": "coordinate_radius_connected_components_with_min_site_merge",
        "region_radius_miles": prep["region_radius_miles"],
        "min_region_site_count": prep["min_region_site_count"],
        "min_colony_days_observed": round(min(score.feature.days_observed for score in reporting), 2) if reporting else 0,
        "max_colony_days_observed": round(max(score.feature.days_observed for score in reporting), 2) if reporting else 0,
        "weight_event_count": sum(score.feature.weight_event_count for score in scores),
        "colonies_with_weight_events": sum(1 for score in scores if score.feature.weight_event_count),
        "max_reporting_gap_days": settings["max_reporting_gap_days"],
        "not_reporting_colony_count": len(not_reporting),
        "not_reporting_colonies": not_reporting,
    }
    return sorted(scores, key=lambda score: (score.region_id, -score.score, score.colony_id)), metadata


def _require_data_dir(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"Missing {label} data directory: {path}")


def _reporting_gaps(
    sensor_readings: list[SensorReading],
    hives: dict[str, HiveConfig],
    colony_sides: tuple[str, ...],
    reference_at: datetime,
    max_gap_days: float,
) -> list[dict]:
    """Configured colonies whose last reading is more than ``max_gap_days`` old.

    "Old" is measured against ``reference_at`` -- the newest reading anywhere in
    the cache -- because the system clock is deliberately never consulted (see
    the timestamp invariant). The consequence is that an apiary-wide outage
    cannot be detected this way: every colony is equally stale, so no colony is
    late relative to the others. That case surfaces as a stale window end in the
    report header instead.

    A colony with no readings at all is included with ``last_reading_at: None``:
    a hive that never appears is exactly as invisible as one that went quiet.
    """
    last_by_colony: dict[str, SensorReading] = {}
    for reading in sensor_readings:
        current = last_by_colony.get(reading.colony_id)
        if current is None or reading.observed_at > current.observed_at:
            last_by_colony[reading.colony_id] = reading

    gaps: list[dict] = []
    for hive_id, hive in sorted(hives.items()):
        for colony_side in colony_sides:
            colony_id = f"{hive_id}:{colony_side}"
            last = last_by_colony.get(colony_id)
            gap_days = None if last is None else (reference_at - last.observed_at).total_seconds() / 86400
            if gap_days is not None and gap_days <= max_gap_days:
                continue
            gaps.append(
                {
                    "colony_id": colony_id,
                    "hive_id": hive_id,
                    "colony_side": colony_side,
                    "region_id": hive.region_id,
                    "last_reading_at": last.observed_at.isoformat() if last else None,
                    "gap_days": round(gap_days, 2) if gap_days is not None else None,
                }
            )
    return gaps


def _not_reporting_flag(gap: dict) -> str:
    if gap["last_reading_at"] is None:
        return "Not reporting: no sensor readings at all in the cached history."
    return (
        f"Not reporting: no sensor readings for {gap['gap_days']:.1f} days "
        f"(last reading {gap['last_reading_at']})."
    )


def _apply_reporting_gaps(
    scores: list[ColonyScore],
    gaps: list[dict],
    window_start: str,
    window_end: str,
) -> list[ColonyScore]:
    """Mark scored colonies that have fallen behind, and materialise a score for
    colonies with no readings in the window at all.

    Without the second half a silent hive simply disappears from the report --
    the most dangerous failure mode there is, because an absent colony looks
    exactly like a healthy one that was never mentioned. Silent colonies carry
    ``sample_count == 0`` so peer statistics, coverage metadata, and sister
    comparisons can all exclude them; they were never in the peer pool, since
    the feature builder only sees colonies with readings in the window.
    """
    by_colony = {gap["colony_id"]: gap for gap in gaps}
    scored_ids = {score.colony_id for score in scores}

    for score in scores:
        gap = by_colony.get(score.colony_id)
        if gap is None:
            continue
        score.reporting_gap_days = gap["gap_days"]
        score.flags.insert(0, _not_reporting_flag(gap))
        score.status = "underperforming"

    for colony_id, gap in sorted(by_colony.items()):
        if colony_id in scored_ids:
            continue
        scores.append(_not_reporting_score(gap, window_start, window_end))
    return scores


def _not_reporting_score(gap: dict, window_start: str, window_end: str) -> ColonyScore:
    last_at = datetime.fromisoformat(gap["last_reading_at"]) if gap["last_reading_at"] else None
    feature = ColonyFeatures(
        colony_id=gap["colony_id"],
        region_id=gap["region_id"],
        hive_id=gap["hive_id"],
        colony_side=gap["colony_side"],
        sample_count=0,
        excluded_reading_count=0,
        data_quality_flags=[],
        start_at=last_at or datetime.fromisoformat(window_start),
        end_at=last_at or datetime.fromisoformat(window_end),
        days_observed=0.0,
        latest_weight_kg=0.0,
        weight_delta_kg=0.0,
        weight_pct_change=0.0,
        weight_slope_kg_per_day=0.0,
        weight_slope_pct_per_day=0.0,
        favorable_weather_window_count=0,
        poor_weather_window_count=0,
        favorable_weather_weight_slope_pct_per_day=0.0,
        poor_weather_weight_loss_pct=0.0,
        avg_weather_temp_f=None,
        avg_weather_humidity_pct=None,
        rainy_weather_reading_pct=None,
        cloudy_weather_reading_pct=None,
        dominant_weather_overview=None,
    )
    return ColonyScore(
        colony_id=gap["colony_id"],
        region_id=gap["region_id"],
        hive_id=gap["hive_id"],
        colony_side=gap["colony_side"],
        # Not a peer-relative judgement -- there is nothing to compare. The zero
        # says "unscored"; the status and flag carry the meaning.
        score=0.0,
        status="underperforming",
        comparisons=[],
        feature=feature,
        flags=[_not_reporting_flag(gap)],
        reporting_gap_days=gap["gap_days"],
    )


def _score_features(
    features: list[ColonyFeatures],
    settings: dict[str, float],
    metrics: list[Metric] = METRICS,
) -> list[ColonyScore]:
    by_region: dict[str, list[ColonyFeatures]] = defaultdict(list)
    for feature in features:
        by_region[feature.region_id].append(feature)

    scores: list[ColonyScore] = []
    for region_id in sorted(by_region):
        region_features = sorted(by_region[region_id], key=lambda feature: feature.colony_id)
        scores.extend(_score_region_features(region_features, settings, metrics))
    return scores


def _score_region_features(
    features: list[ColonyFeatures],
    settings: dict[str, float],
    metrics: list[Metric] = METRICS,
) -> list[ColonyScore]:
    scores: list[ColonyScore] = []
    threshold = settings["zscore_badness_threshold"]
    drop_threshold = settings["weight_drop_pct_threshold"]
    # Defaults mirror data_loader.load_hive_config so callers that hand-build a
    # settings dict (spikes, tests) keep working.
    window_days = float(settings.get("rolling_window_days", 7.0))
    quality_day_share = float(settings.get("quality_issue_day_share_threshold", 0.30))

    for feature in features:
        comparisons: list[MetricComparison] = []
        raw_score = 0.0
        total_weight = 0.0

        for metric in metrics:
            eligible_peers = _eligible_metric_peers(features, metric)
            if feature not in eligible_peers or len(eligible_peers) < MIN_PEER_COUNT:
                continue
            values = [float(getattr(peer, metric.name)) for peer in eligible_peers]
            peer_mean = statistics.fmean(values)
            peer_std = stddev(values)
            value = float(getattr(feature, metric.name))
            badness_z = _badness_z(value, peer_mean, peer_std, metric.direction)
            metric_weight = metric.weight
            total_weight += metric_weight
            raw_score += max(0.0, badness_z) * metric_weight
            # Per-metric peer-pool confidence. min_sample_attr gating (weather
            # metrics) can shrink one metric's pool below the region size; at
            # n<=2 Samuelson's inequality pins |badness_z| at the sqrt(n-1)
            # bound, so the z carries only sign, not magnitude. Label it (do not
            # reweight): a small pool is a property of the region, not a fault.
            peer_count = len(eligible_peers)
            z_bound = math.sqrt(peer_count - 1)
            confidence = _metric_confidence(peer_count, badness_z, peer_std, z_bound)
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
                    peer_count=peer_count,
                    z_bound=z_bound,
                    confidence=confidence,
                )
            )

        score = round(min(100.0, (raw_score / max(total_weight, 0.001)) * BADNESS_Z_SCORE_SCALE), 1)
        flags = _flags(feature, comparisons, threshold, drop_threshold)
        status = _status(
            score,
            flags,
            quality_flags_material=_quality_issue_days_material(
                feature.data_quality_issue_days, window_days, quality_day_share
            ),
        )
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
                weight_events=feature.weight_events,
            )
        )

    return sorted(scores, key=lambda score: (-score.score, score.colony_id))


def _eligible_metric_peers(features: list[ColonyFeatures], metric: Metric) -> list[ColonyFeatures]:
    if not metric.min_sample_attr:
        return features
    return [feature for feature in features if getattr(feature, metric.min_sample_attr) >= metric.min_sample_count]


def _metric_confidence(peer_count: int, badness_z: float, peer_std: float, z_bound: float) -> str:
    """"low" when this metric's peer pool is too small for the z to carry
    magnitude: n<=2 (bound collapses to <=1.0), or the z is pinned at the
    Samuelson bound sqrt(n-1) — the degenerate case documented in metrics.py."""
    if peer_count <= 2:
        return "low"
    if peer_std > 0 and z_bound > 0 and abs(badness_z) >= 0.999 * z_bound:
        return "low"
    return "normal"


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
    for description in feature.weight_event_descriptions:
        flags.append(description)
    if feature.weight_pct_change <= -drop_threshold:
        flags.append(f"Weight dropped {abs(feature.weight_pct_change):.1f}% during the window.")
    for comparison in comparisons:
        if comparison.badness_z >= threshold:
            flags.append(
                f"{comparison.label} is {comparison.badness_z:.1f} standard deviations worse than peers."
            )
    low_confidence = [c for c in comparisons if c.confidence == "low"]
    for comparison in low_confidence[:2]:
        flags.append(
            f"Low confidence: {comparison.label} compared against only "
            f"{comparison.peer_count} eligible peers (z-score bounded at "
            f"±{comparison.z_bound:.1f})."
        )
    if feature.excluded_reading_count:
        flags.append(f"Excluded {feature.excluded_reading_count} sensor readings due to data quality checks.")
    for data_flag in feature.data_quality_flags[:2]:
        flags.append(f"Data quality: {data_flag}")
    return flags


def _quality_issue_days_material(issue_days: int, window_days: float, day_share: float) -> bool:
    """Whether a colony's data-quality problems are widespread enough to matter.

    Every deployment produces occasional bad readings; one glitch says nothing
    about the colony and should not put it on the inspection list. A fault that
    recurs across more than ``day_share`` of the scoring window is a different
    thing -- the sensor (or the hive) needs attention, and the colony's metrics
    are built on thinner data than its peers'. The default 30% share means more
    than 2 affected days in a 7-day window.
    """
    if window_days <= 0:
        return False
    return issue_days > day_share * window_days


def _status(score: float, flags: list[str], quality_flags_material: bool = True) -> str:
    quality_flags = [flag for flag in flags if flag.startswith("Data quality:") or flag.startswith("Excluded ")]
    event_flags = [flag for flag in flags if flag.startswith("Likely ")]
    # A small peer pool is a property of the region, not a colony problem, so
    # low-confidence flags drive neither the underperforming nor the watch cut.
    confidence_flags = [flag for flag in flags if flag.startswith("Low confidence:")]
    # "Not reporting" is a hardware fact, not a peer-relative performance signal;
    # _apply_reporting_gaps sets the status for those colonies directly.
    reporting_flags = [flag for flag in flags if flag.startswith("Not reporting:")]
    performance_flags = [
        flag
        for flag in flags
        if flag not in quality_flags
        and flag not in event_flags
        and flag not in confidence_flags
        and flag not in reporting_flags
    ]
    # Quality flags are always reported, but only drive the watch cut when the
    # underlying issues span a meaningful share of the window; they are never a
    # performance flag either way.
    watch_quality_flags = quality_flags if quality_flags_material else []
    if score >= UNDERPERFORMING_SCORE or len(performance_flags) >= UNDERPERFORMING_FLAG_COUNT:
        return "underperforming"
    if score >= WATCH_SCORE or performance_flags or watch_quality_flags:
        return "watch"
    return "normal"


