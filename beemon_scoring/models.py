from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class HiveConfig:
    hive_id: str
    device_uid: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class SensorReading:
    hive_id: str
    colony_side: str
    device_uid: str
    timestamp: int
    observed_at: datetime
    weight_lb: float
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
    temperature_f: float
    pressure_hpa: float | None
    cloudiness_pct: float | None
    humidity_pct: float | None
    weather_code: int | None
    overview: str


@dataclass
class ColonyFeatures:
    colony_id: str
    hive_id: str
    colony_side: str
    sample_count: int
    start_at: datetime
    end_at: datetime
    days_observed: float
    latest_weight_lb: float
    weight_delta_lb: float
    weight_pct_change: float
    weight_slope_lb_per_day: float
    weight_slope_pct_per_day: float
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
    hive_id: str
    colony_side: str
    score: float
    status: str
    comparisons: list[MetricComparison]
    feature: ColonyFeatures
    flags: list[str] = field(default_factory=list)

