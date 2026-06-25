from __future__ import annotations

from dataclasses import dataclass


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
# at least 10 colonies. MIN_REGION_SITE_COUNT (hive_config.py) keeps regions
# from shrinking to a single site (n=2), where that bound collapses to
# exactly 1.0 and z-scores stop carrying any real magnitude information.
BADNESS_Z_SCORE_SCALE = 35.0

METRICS = [
    Metric(
        name="latest_weight_kg",
        label="current colony weight",
        direction="higher_is_better",
        weight=0.30,
        unit="kg",
    ),
    Metric(
        name="weight_pct_change",
        label="7-day weight percent change",
        direction="higher_is_better",
        weight=0.17,
        unit="%",
    ),
    Metric(
        name="weight_slope_pct_per_day",
        label="weight percent trend",
        direction="higher_is_better",
        weight=0.09,
        unit="%/day",
    ),
    Metric(
        name="favorable_weather_weight_slope_pct_per_day",
        label="favorable-weather weight percent trend",
        direction="higher_is_better",
        weight=0.06,
        unit="%/day",
        min_sample_attr="favorable_weather_window_count",
        min_sample_count=1,
    ),
    Metric(
        name="poor_weather_weight_loss_pct",
        label="poor-weather weight loss",
        direction="lower_is_better",
        weight=0.04,
        unit="%",
        min_sample_attr="poor_weather_window_count",
        min_sample_count=1,
    ),
    Metric(
        name="internal_temp_std_f",
        label="temperature instability",
        direction="lower_is_better",
        weight=0.13,
        unit="F",
    ),
    Metric(
        name="avg_brood_temp_deviation_f",
        label="possible brood-temperature variation",
        direction="lower_is_better",
        weight=0.10,
        unit="F",
    ),
    Metric(
        name="high_humidity_reading_pct",
        label="high-humidity exposure",
        direction="lower_is_better",
        weight=0.06,
        unit="%",
    ),
    Metric(
        name="internal_humidity_std_pct",
        label="humidity instability",
        direction="lower_is_better",
        weight=0.05,
        unit="%",
    ),
]
