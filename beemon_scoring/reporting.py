from __future__ import annotations

import json
from dataclasses import asdict

from .models import ColonyScore, MetricComparison


def scores_to_json(scores: list[ColonyScore], metadata: dict[str, object]) -> str:
    return json.dumps(
        {
            "metadata": metadata,
            "colonies": [asdict(score) for score in scores],
        },
        indent=2,
        default=str,
    )


def build_text_report(scores: list[ColonyScore], metadata: dict[str, object]) -> str:
    lines = [
        "BeeMon Regional Hive Scoring MVP",
        "=" * 34,
        f"Window: {metadata['start_at']} to {metadata['end_at']} ({metadata['window_days']} days)",
        f"Colonies compared: {metadata['colony_count']}",
        f"Sensor coverage per colony: {metadata['min_colony_days_observed']} to {metadata['max_colony_days_observed']} days",
        "",
    ]

    if not scores:
        return "\n".join(lines + ["No colony scores available."])

    top = scores[0]
    if top.status == "normal":
        lines.append("No colony is currently underperforming relative to this peer group.")
    else:
        lines.append(_natural_language_summary(top))

    lines.extend(["", "Ranked colonies:"])
    for index, score in enumerate(scores, start=1):
        feature = score.feature
        lines.append(
            f"{index}. {score.colony_id} - {score.status} "
            f"(underperformance score {score.score:.1f}/100, "
            f"weight {feature.weight_delta_lb:+.2f} lb ({feature.weight_pct_change:+.1f}%) over {feature.days_observed:.1f} days)"
        )
        for comparison in _top_drivers(score.comparisons, limit=3):
            lines.append(
                f"   - {comparison.label}: {comparison.value:.2f}{_unit(comparison)} "
                f"vs peer avg {comparison.peer_mean:.2f}{_unit(comparison)} "
                f"(badness z {comparison.badness_z:.1f})"
            )
        if score.flags:
            for flag in score.flags[:3]:
                lines.append(f"   - Flag: {flag}")

    return "\n".join(lines)


def _natural_language_summary(score: ColonyScore) -> str:
    feature = score.feature
    drivers = _top_drivers(score.comparisons, limit=2)
    driver_text = " and ".join(driver.label for driver in drivers) if drivers else "overall peer deviation"
    weather = ""
    if feature.avg_weather_temp_f is not None:
        weather = (
            f" Weather during the window averaged {feature.avg_weather_temp_f:.1f} F"
            f" with {feature.rainy_weather_reading_pct or 0:.0f}% rainy readings."
        )

    return (
        f"{score.colony_id} is the most concerning colony in this regional peer group. "
        f"It is classified as {score.status} with an underperformance score of {score.score:.1f}/100. "
        f"The main drivers are {driver_text}. Its weight changed {feature.weight_delta_lb:+.2f} lb ({feature.weight_pct_change:+.1f}%) "
        f"over {feature.days_observed:.1f} days, compared with the peer-relative expectations below."
        f"{weather}"
    )


def _top_drivers(comparisons: list[MetricComparison], limit: int) -> list[MetricComparison]:
    positive = [comparison for comparison in comparisons if comparison.badness_z > 0]
    return sorted(positive, key=lambda comparison: comparison.badness_z, reverse=True)[:limit]


def _unit(comparison: MetricComparison) -> str:
    return f" {comparison.unit}" if comparison.unit else ""

