from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class HiveConfig:
    hive_id: str
    region_id: str
    device_uid: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class SensorReading:
    hive_id: str
    region_id: str
    colony_side: str
    device_uid: str
    timestamp: int
    observed_at: datetime
    weight_kg: float
    internal_temp_f: float
    internal_humidity_pct: float
    external_temp_f: float | None
    external_humidity_pct: float | None

    @property
    def colony_id(self) -> str:
        return f"{self.hive_id}:{self.colony_side}"


@dataclass(frozen=True)
class WeatherReading:
    hive_id: str
    observed_date: date
    clock_time: str
    temperature_f: float | None
    pressure_hpa: float | None
    cloudiness_pct: float | None
    humidity_pct: float | None
    weather_code: int | None
    overview: str


@dataclass
class ColonyFeatures:
    colony_id: str
    region_id: str
    hive_id: str
    colony_side: str
    sample_count: int
    excluded_reading_count: int
    data_quality_flags: list[str]
    start_at: datetime
    end_at: datetime
    days_observed: float
    latest_weight_kg: float
    weight_delta_kg: float
    weight_pct_change: float
    weight_slope_kg_per_day: float
    weight_slope_pct_per_day: float
    favorable_weather_window_count: int
    poor_weather_window_count: int
    favorable_weather_weight_slope_pct_per_day: float
    poor_weather_weight_loss_pct: float
    avg_internal_temp_f: float
    internal_temp_std_f: float
    avg_brood_temp_deviation_f: float
    avg_internal_humidity_pct: float
    internal_humidity_std_pct: float
    high_humidity_reading_pct: float
    low_humidity_reading_pct: float
    avg_external_temp_f: float | None
    avg_external_humidity_pct: float | None
    avg_weather_temp_f: float | None
    avg_weather_humidity_pct: float | None
    rainy_weather_reading_pct: float | None
    cloudy_weather_reading_pct: float | None
    dominant_weather_overview: str | None


@dataclass
class MetricComparison:
    metric: str
    label: str
    value: float
    peer_mean: float
    peer_std: float
    badness_z: float
    weight: float
    unit: str = ""


@dataclass
class ColonyScore:
    colony_id: str
    region_id: str
    hive_id: str
    colony_side: str
    score: float
    status: str
    comparisons: list[MetricComparison]
    feature: ColonyFeatures
    flags: list[str] = field(default_factory=list)


@dataclass
class RegionColonyHighlight:
    colony_id: str
    hive_id: str
    colony_side: str
    score: float
    status: str


@dataclass
class RegionSummary:
    region_id: str
    site_ids: list[str]
    site_count: int
    colony_count: int
    normal_count: int
    watch_count: int
    underperforming_count: int
    performing_well_colonies: list[RegionColonyHighlight]
    underperforming_colonies: list[RegionColonyHighlight]
    watch_colonies: list[RegionColonyHighlight]
    summary: str


@dataclass
class SisterMetricComparison:
    metric: str
    label: str
    unit: str
    left_value: float
    right_value: float
    worse_side: str | None
    raw_gap: float
    normalized_gap: float
    impact: float


@dataclass
class SisterSiteComparison:
    hive_id: str
    left_colony_id: str | None
    right_colony_id: str | None
    weaker_side: str | None
    status: str
    left_sister_score: float
    right_sister_score: float
    metric_comparisons: list[SisterMetricComparison]
    summary: str
