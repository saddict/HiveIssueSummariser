from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime

from .models import ColonyScore, MetricComparison, RegionColonyHighlight, RegionSummary


def scores_to_json(scores: list[ColonyScore], metadata: dict[str, object]) -> str:
    region_summaries = build_region_summaries(scores)
    return json.dumps(
        {
            "metadata": metadata,
            "regions": [asdict(summary) for summary in region_summaries],
            "colonies": [asdict(score) for score in scores],
        },
        indent=2,
        default=str,
    )


def build_region_summaries(scores: list[ColonyScore]) -> list[RegionSummary]:
    by_region = _scores_by_region(scores)
    summaries: list[RegionSummary] = []

    for region_id, region_scores in by_region.items():
        sorted_by_score = sorted(region_scores, key=lambda score: (score.score, score.colony_id))
        performing_well_scores = _performing_well_scores(sorted_by_score)
        underperforming_scores = sorted(
            [score for score in region_scores if score.status == "underperforming"],
            key=lambda score: (-score.score, score.colony_id),
        )
        watch_scores = sorted(
            [score for score in region_scores if score.status == "watch"],
            key=lambda score: (-score.score, score.colony_id),
        )

        summaries.append(
            RegionSummary(
                region_id=region_id,
                site_ids=sorted({score.hive_id for score in region_scores}),
                site_count=len({score.hive_id for score in region_scores}),
                colony_count=len(region_scores),
                normal_count=sum(1 for score in region_scores if score.status == "normal"),
                watch_count=len(watch_scores),
                underperforming_count=len(underperforming_scores),
                performing_well_colonies=[_highlight(score) for score in performing_well_scores],
                underperforming_colonies=[_highlight(score) for score in underperforming_scores],
                watch_colonies=[_highlight(score) for score in watch_scores],
                summary=_region_summary_text(region_id, performing_well_scores, underperforming_scores, watch_scores),
            )
        )

    return summaries


def build_text_report(scores: list[ColonyScore], metadata: dict[str, object]) -> str:
    region_summaries = build_region_summaries(scores)
    lines = [
        "BeeMon Regional Hive Scoring MVP",
        "=" * 34,
        f"Window: {metadata['start_at']} to {metadata['end_at']} ({metadata['window_days']} days)",
        f"Regions compared: {metadata.get('region_count', len(region_summaries))}",
        f"Colonies compared: {metadata['colony_count']}",
        f"Sensor coverage per colony: {metadata['min_colony_days_observed']} to {metadata['max_colony_days_observed']} days",
        f"Valid sensor readings: {metadata['valid_sensor_reading_count']} of {metadata['sensor_reading_count']} "
        f"({metadata['excluded_sensor_reading_count']} excluded, {metadata['data_quality_issue_count']} quality notes)",
        "",
    ]

    if not scores:
        return "\n".join(lines + ["No colony scores available."])

    if all(score.status == "normal" for score in scores):
        lines.append("No colony is currently underperforming relative to its regional peer group.")
    else:
        lines.append("Colonies are scored within their configured regions. Regional highlights are listed below.")

    lines.extend(["", "Regional highlights:"])
    for summary in region_summaries:
        site_list = ", ".join(summary.site_ids)
        site_label = "site" if summary.site_count == 1 else "sites"
        colony_label = "colony" if summary.colony_count == 1 else "colonies"
        lines.append(f"{summary.region_id} (sites: {site_list}; {summary.site_count} {site_label}, {summary.colony_count} {colony_label})")
        lines.append(f"   - Performing well: {_format_highlights(summary.performing_well_colonies)}")
        lines.append(f"   - Underperforming: {_format_highlights(summary.underperforming_colonies)}")
        if summary.watch_colonies:
            lines.append(f"   - Watch: {_format_highlights(summary.watch_colonies)}")

    lines.extend(["", "Ranked colonies by region:"])
    for region_id, region_scores in _scores_by_region(scores).items():
        lines.append(f"{region_id}:")
        for index, score in enumerate(sorted(region_scores, key=lambda item: (-item.score, item.colony_id)), start=1):
            feature = score.feature
            lines.append(
                f"{index}. {score.colony_id} - {score.status} "
                f"(underperformance score {score.score:.1f}/100, "
                f"weight {feature.weight_delta_kg:+.2f} kg ({feature.weight_pct_change:+.1f}%) over {feature.days_observed:.1f} days)"
            )
            for comparison in _top_drivers(score.comparisons, limit=3):
                lines.append(
                    f"   - {comparison.label}: {comparison.value:.2f}{_unit(comparison)} "
                    f"vs peer avg {comparison.peer_mean:.2f}{_unit(comparison)} "
                    f"(badness z {comparison.badness_z:.1f})"
                )
            if score.weight_events:
                lines.append("   Weight events (newest first):")
                for ev in score.weight_events:
                    lines.append(_format_weight_event_row(ev))
            non_event_flags = [f for f in score.flags if not f.startswith("Likely ")]
            for flag in non_event_flags[:4]:
                lines.append(f"   - Flag: {flag}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _scores_by_region(scores: list[ColonyScore]) -> dict[str, list[ColonyScore]]:
    by_region: dict[str, list[ColonyScore]] = defaultdict(list)
    for score in scores:
        by_region[score.region_id].append(score)
    return {region_id: by_region[region_id] for region_id in sorted(by_region)}


def _performing_well_scores(sorted_by_score: list[ColonyScore]) -> list[ColonyScore]:
    return [score for score in sorted_by_score if score.status == "normal"][:2]


def _highlight(score: ColonyScore) -> RegionColonyHighlight:
    return RegionColonyHighlight(
        colony_id=score.colony_id,
        hive_id=score.hive_id,
        colony_side=score.colony_side,
        score=score.score,
        status=score.status,
    )


def _region_summary_text(
    region_id: str,
    performing_well_scores: list[ColonyScore],
    underperforming_scores: list[ColonyScore],
    watch_scores: list[ColonyScore],
) -> str:
    strongest = ", ".join(score.colony_id for score in performing_well_scores) if performing_well_scores else "no clear leaders yet"
    if underperforming_scores:
        concerns = ", ".join(score.colony_id for score in underperforming_scores)
        return f"In region {region_id}, the strongest colonies are {strongest}. The main underperformers are {concerns}."
    if watch_scores:
        watch_list = ", ".join(score.colony_id for score in watch_scores)
        return f"In region {region_id}, the strongest colonies are {strongest}. Watch colonies are {watch_list}."
    return f"In region {region_id}, the strongest colonies are {strongest}. No colony is currently underperforming in this region."


def _format_highlights(highlights: list[RegionColonyHighlight]) -> str:
    if not highlights:
        return "none"
    return ", ".join(f"{highlight.colony_id} ({highlight.score:.1f}, {highlight.status})" for highlight in highlights)


def _top_drivers(comparisons: list[MetricComparison], limit: int) -> list[MetricComparison]:
    positive = [comparison for comparison in comparisons if comparison.badness_z > 0]
    return sorted(positive, key=lambda comparison: comparison.badness_z, reverse=True)[:limit]


def _unit(comparison: MetricComparison) -> str:
    return f" {comparison.unit}" if comparison.unit else ""


def _format_weight_event_row(ev: dict) -> str:
    dt = datetime.fromisoformat(ev["observed_at"])
    time_str = dt.strftime("%Y-%m-%d %H:%M") + " " + ev["observed_at"][-6:]
    kind_str = ev["kind"].ljust(12)
    delta_str = f"{ev['delta_kg']:+.3f} kg".rjust(11)
    pct_str = f"({ev['pct_change']:+.1f}%)".rjust(8)
    range_str = f"{ev['before_kg']:.3f} → {ev['after_kg']:.3f} kg"
    return f"     {time_str}  {kind_str}  {delta_str}  {pct_str}  {range_str}"
