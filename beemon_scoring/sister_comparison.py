from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict

from .models import ColonyScore, SisterMetricComparison, SisterSiteComparison
from .scoring import METRICS


SAME_SIDE_LABELS = {"L": "left", "R": "right"}
SIGNIFICANT_WEIGHT_TREND_IMPACT = 4.0
WEIGHT_TREND_METRICS = {
    "weight_pct_change",
    "weight_slope_pct_per_day",
    "favorable_weather_weight_slope_pct_per_day",
    "poor_weather_weight_loss_pct",
}


def build_sister_comparisons(scores: list[ColonyScore]) -> list[SisterSiteComparison]:
    by_site: dict[str, list[ColonyScore]] = defaultdict(list)
    for score in scores:
        by_site[score.hive_id].append(score)

    comparisons: list[SisterSiteComparison] = []
    for site_id, site_scores in sorted(by_site.items()):
        by_side = {score.colony_side: score for score in site_scores}
        left = by_side.get("L")
        right = by_side.get("R")
        if left is None or right is None:
            comparisons.append(
                SisterSiteComparison(
                    hive_id=site_id,
                    left_colony_id=left.colony_id if left else None,
                    right_colony_id=right.colony_id if right else None,
                    weaker_side=None,
                    status="incomplete",
                    left_sister_score=0.0,
                    right_sister_score=0.0,
                    metric_comparisons=[],
                    summary="This site needs both left and right colony readings before sister comparison is possible.",
                )
            )
            continue

        metric_comparisons = _compare_sister_metrics(left, right)
        left_score = round(sum(item.impact for item in metric_comparisons if item.worse_side == "L"), 1)
        right_score = round(sum(item.impact for item in metric_comparisons if item.worse_side == "R"), 1)
        weaker_side, status = _sister_status(left_score, right_score)
        comparisons.append(
            SisterSiteComparison(
                hive_id=site_id,
                left_colony_id=left.colony_id,
                right_colony_id=right.colony_id,
                weaker_side=weaker_side,
                status=status,
                left_sister_score=left_score,
                right_sister_score=right_score,
                metric_comparisons=metric_comparisons,
                summary=_summary(site_id, weaker_side, status, left_score, right_score, metric_comparisons),
            )
        )

    return comparisons


def sister_comparisons_to_json(comparisons: list[SisterSiteComparison], metadata: dict[str, object]) -> str:
    return json.dumps(
        {
            "metadata": metadata | {"sister_site_count": len(comparisons)},
            "sites": [asdict(comparison) for comparison in comparisons],
        },
        indent=2,
        default=str,
    )


def build_sister_text_report(comparisons: list[SisterSiteComparison], metadata: dict[str, object]) -> str:
    lines = [
        "BeeMon Sister-Colony Comparison",
        "=" * 33,
        f"Window: {metadata['start_at']} to {metadata['end_at']} ({metadata['window_days']} days)",
        f"Sites compared: {len(comparisons)}",
        "",
    ]
    for comparison in comparisons:
        lines.append(
            f"{comparison.hive_id}: {comparison.status} "
            f"(L score {comparison.left_sister_score:.1f}, R score {comparison.right_sister_score:.1f})"
        )
        lines.append(f"  {comparison.summary}")
        for metric in _top_sister_drivers(comparison.metric_comparisons, limit=3):
            lines.append(
                f"  - {metric.label}: L {metric.left_value:.2f}{_unit(metric.unit)} vs "
                f"R {metric.right_value:.2f}{_unit(metric.unit)}; "
                f"{SAME_SIDE_LABELS.get(metric.worse_side or '', 'neither side')} worse"
                f" by {metric.raw_gap:.2f}{_unit(metric.unit)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _compare_sister_metrics(left: ColonyScore, right: ColonyScore) -> list[SisterMetricComparison]:
    left_metric_map = {comparison.metric: comparison for comparison in left.comparisons}
    right_metric_map = {comparison.metric: comparison for comparison in right.comparisons}
    comparisons: list[SisterMetricComparison] = []

    for metric in METRICS:
        name = str(metric["name"])
        if name not in left_metric_map or name not in right_metric_map:
            continue

        left_value = float(getattr(left.feature, name))
        right_value = float(getattr(right.feature, name))
        raw_gap = _raw_badness_gap(left_value, right_value, str(metric["direction"]))
        worse_side = "L" if raw_gap > 0 else "R" if raw_gap < 0 else None
        peer_std = _mean_positive_std(left_metric_map[name].peer_std, right_metric_map[name].peer_std)
        normalized_gap = abs(raw_gap) / peer_std if peer_std else 0.0
        impact = min(100.0, normalized_gap * float(metric["weight"]) * 35)

        comparisons.append(
            SisterMetricComparison(
                metric=name,
                label=str(metric["label"]),
                unit=str(metric.get("unit", "")),
                left_value=left_value,
                right_value=right_value,
                worse_side=worse_side,
                raw_gap=abs(raw_gap),
                normalized_gap=normalized_gap,
                impact=impact,
            )
        )

    return sorted(comparisons, key=lambda item: item.impact, reverse=True)


def _raw_badness_gap(left_value: float, right_value: float, direction: str) -> float:
    if direction == "higher_is_better":
        return right_value - left_value
    return left_value - right_value


def _mean_positive_std(left_std: float, right_std: float) -> float:
    values = [value for value in (left_std, right_std) if value > 0]
    return sum(values) / len(values) if values else 0.0


def _sister_status(left_score: float, right_score: float) -> tuple[str | None, str]:
    gap = abs(left_score - right_score)
    if gap < 5:
        return None, "similar"
    weaker_side = "L" if left_score > right_score else "R"
    if max(left_score, right_score) >= 25 or gap >= 15:
        return weaker_side, f"{SAME_SIDE_LABELS[weaker_side]} colony notably weaker"
    return weaker_side, f"{SAME_SIDE_LABELS[weaker_side]} colony mildly weaker"


def _summary(
    site_id: str,
    weaker_side: str | None,
    status: str,
    left_score: float,
    right_score: float,
    metric_comparisons: list[SisterMetricComparison],
) -> str:
    if weaker_side is None:
        trend_warning = _opposing_weight_trend_warning(None, metric_comparisons)
        return f"The two colonies at {site_id} look broadly similar on the measured scoring features.{trend_warning}"
    drivers = [metric.label for metric in _top_sister_drivers(metric_comparisons, limit=2) if metric.worse_side == weaker_side]
    driver_text = " and ".join(drivers) if drivers else "the measured scoring features"
    trend_warning = _opposing_weight_trend_warning(weaker_side, metric_comparisons)
    return (
        f"The {SAME_SIDE_LABELS[weaker_side]} colony is weaker than its sister colony at {site_id}. "
        f"The main sister-level drivers are {driver_text}. "
        f"Sister scores are L {left_score:.1f} vs R {right_score:.1f}.{trend_warning}"
    )


def _opposing_weight_trend_warning(
    weaker_side: str | None,
    metric_comparisons: list[SisterMetricComparison],
) -> str:
    trend_metrics = [
        metric
        for metric in metric_comparisons
        if metric.metric in WEIGHT_TREND_METRICS
        and metric.worse_side is not None
        and (weaker_side is None or metric.worse_side != weaker_side)
        and metric.impact >= SIGNIFICANT_WEIGHT_TREND_IMPACT
        and _is_negative_weight_movement(metric)
    ]
    if not trend_metrics:
        return ""

    side = trend_metrics[0].worse_side
    side_metrics = [metric for metric in trend_metrics if metric.worse_side == side]
    labels = [metric.label for metric in side_metrics[:2]]
    if len(labels) == 1:
        metric_text = labels[0]
    else:
        metric_text = f"{labels[0]} and {labels[1]}"
    return (
        f" However, the {SAME_SIDE_LABELS[side]} colony has a significant negative weight trend "
        f"on {metric_text}, so that decline should still be watched."
    )


def _is_negative_weight_movement(metric: SisterMetricComparison) -> bool:
    worse_value = metric.left_value if metric.worse_side == "L" else metric.right_value
    if metric.metric == "poor_weather_weight_loss_pct":
        return worse_value > 0
    return worse_value < 0


def _top_sister_drivers(
    metric_comparisons: list[SisterMetricComparison],
    limit: int,
) -> list[SisterMetricComparison]:
    return [metric for metric in metric_comparisons if metric.worse_side is not None and metric.impact > 0][:limit]


def _unit(unit: str) -> str:
    return f" {unit}" if unit else ""
