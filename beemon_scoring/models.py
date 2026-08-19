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
    display_name: str | None = None
    county: str | None = None
    state: str | None = None
    region_label: str | None = None


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
    avg_weather_temp_f: float | None
    avg_weather_humidity_pct: float | None
    rainy_weather_reading_pct: float | None
    cloudy_weather_reading_pct: float | None
    dominant_weather_overview: str | None
    weight_event_count: int = 0
    weight_event_descriptions: list[str] = field(default_factory=list)
    weight_events: list[dict] = field(default_factory=list)
    segment_count: int = 1
    # Distinct calendar days on which any data-quality issue was recorded. A
    # fault that spans a large share of the window is a colony-level problem;
    # an isolated bad reading is not. See scoring._quality_issue_days_material.
    data_quality_issue_days: int = 0


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
    # Peer-pool size this metric was scored against, and the Samuelson bound
    # sqrt(peer_count - 1) on |badness_z|. When the pool is degenerate (n<=2, or
    # z pinned at the bound) confidence is "low": the z-score carries only sign,
    # not magnitude. See scoring._score_region_features and the metrics.py
    # BADNESS_Z_SCORE_SCALE note.
    peer_count: int = 0
    z_bound: float = 0.0
    confidence: str = "normal"


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
    weight_events: list[dict] = field(default_factory=list)
    # Days between this colony's last reading and the newest reading anywhere in
    # the cache, set only when that gap exceeds MAX_REPORTING_GAP_DAYS. None means
    # the colony is reporting normally. A colony with no readings in the scoring
    # window at all still gets a ColonyScore (score 0, sample_count 0) so a silent
    # hive cannot vanish from the report. See scoring._apply_reporting_gaps.
    reporting_gap_days: float | None = None


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
