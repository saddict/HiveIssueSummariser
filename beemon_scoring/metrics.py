from __future__ import annotations

from dataclasses import dataclass

from .thresholds import number, section


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    direction: str
    weight: float
    unit: str
    min_sample_attr: str | None = None
    min_sample_count: int = 2


# A weighted-average badness z-score of 1.0 (one std dev worse than peers)
# scales to 35 points; a full 100 would need 2.86 std devs worse. Peer mean
# and std are computed over the region's own colonies, including the colony
# being scored, so by Samuelson's inequality any single metric's z-score is
# bounded by sqrt(n-1) for a region of n colonies -- 2.86 needs a region of
# at least 10 colonies. min_region_site_count (thresholds.toml) keeps regions
# from shrinking to a single site (n=2), where that bound collapses to
# exactly 1.0 and z-scores stop carrying any real magnitude information.
BADNESS_Z_SCORE_SCALE = number("scoring.badness_z_score_scale")

# The catalog below is the system's *structure* -- which features are scored,
# which direction is bad, how they are labelled -- and stays in code. Only the
# weights are tunable, so they come from thresholds.toml ([metric_weights]).
# They are expert-elicited, not learned; robustness is validated in
# spike_weight_sensitivity.py (statuses invariant to +-20% perturbation,
# Kendall tau >= 0.93). Weights are relative -- the score divides by their sum,
# so scaling all of them equally is a no-op.
_WEIGHTS = section("metric_weights")


def _weight(name: str) -> float:
    if name not in _WEIGHTS:
        raise RuntimeError(
            f"No weight for metric '{name}' in thresholds.toml [metric_weights]. "
            f"Every metric in the catalog needs one."
        )
    return float(_WEIGHTS[name])


METRICS = [
    Metric(
        name="latest_weight_kg",
        label="current colony weight",
        direction="higher_is_better",
        weight=_weight("latest_weight_kg"),
        unit="kg",
    ),
    Metric(
        name="weight_pct_change",
        label="weight percent change",
        direction="higher_is_better",
        weight=_weight("weight_pct_change"),
        unit="%",
    ),
    Metric(
        name="weight_slope_pct_per_day",
        label="weight percent trend",
        direction="higher_is_better",
        weight=_weight("weight_slope_pct_per_day"),
        unit="%/day",
    ),
    Metric(
        name="favorable_weather_weight_slope_pct_per_day",
        label="favorable-weather weight percent trend",
        direction="higher_is_better",
        weight=_weight("favorable_weather_weight_slope_pct_per_day"),
        unit="%/day",
        min_sample_attr="favorable_weather_window_count",
        min_sample_count=1,
    ),
    Metric(
        name="poor_weather_weight_loss_pct",
        label="poor-weather weight loss",
        direction="lower_is_better",
        weight=_weight("poor_weather_weight_loss_pct"),
        unit="%",
        min_sample_attr="poor_weather_window_count",
        min_sample_count=1,
    ),
]

_UNUSED_WEIGHTS = sorted(set(_WEIGHTS) - {metric.name for metric in METRICS})
if _UNUSED_WEIGHTS:
    # A weight for a metric that no longer exists is silently ignored otherwise,
    # which is exactly how a "tuned" weight ends up having no effect.
    raise RuntimeError(
        f"thresholds.toml [metric_weights] has entries for metrics that are not "
        f"in the catalog: {', '.join(_UNUSED_WEIGHTS)}."
    )
