from __future__ import annotations

import csv
import importlib.util
import json
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import HiveConfig, SensorReading, WeatherReading


DEFAULT_TIMEZONE = "America/New_York"
EARTH_RADIUS_MILES = 3958.7613


def load_hive_config(config_path: Path) -> tuple[dict[str, HiveConfig], tuple[str, ...], dict[str, float]]:
    spec = importlib.util.spec_from_file_location("hive_config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load config from {config_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    settings = {
        "region_radius_miles": float(getattr(module, "REGION_RADIUS_MILES", 10)),
        "rolling_window_days": float(getattr(module, "ROLLING_WINDOW_DAYS", 7)),
        "zscore_badness_threshold": float(getattr(module, "ZSCORE_BADNESS_THRESHOLD", 1.0)),
        "weight_drop_pct_threshold": float(getattr(module, "WEIGHT_DROP_PCT_THRESHOLD", 5.0)),
    }

    hive_rows = {hive_id: values for hive_id, values in module.HIVES.items()}
    region_ids = _coordinate_region_ids(hive_rows, settings["region_radius_miles"])
    hives = {
        hive_id: HiveConfig(
            hive_id=values["hive_id"],
            region_id=region_ids[hive_id],
            device_uid=str(values["device_uid"]),
            latitude=float(values["latitude"]),
            longitude=float(values["longitude"]),
        )
        for hive_id, values in hive_rows.items()
    }
    return hives, tuple(getattr(module, "COLONY_SIDES", ("L", "R"))), settings


def load_sensor_readings(
    data_dir: Path,
    hives: dict[str, HiveConfig],
    colony_sides: tuple[str, ...],
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[SensorReading]:
    timezone = ZoneInfo(timezone_name)
    readings: list[SensorReading] = []

    for hive in hives.values():
        path = data_dir / f"{hive.hive_id}_SENS.csv"
        if not path.exists():
            continue

        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if str(row["device_uid"]) != hive.device_uid:
                    continue
                sensor_data = _parse_dynamodb_attribute_json(row["sensor_data"])
                timestamp = int(row["timestamp"])
                observed_at = datetime.fromtimestamp(timestamp, tz=timezone)

                for side in colony_sides:
                    weight = sensor_data.get(f"w{side}")
                    temp = sensor_data.get(f"t{side}")
                    humidity = sensor_data.get(f"h{side}")
                    if weight is None or temp is None or humidity is None:
                        continue
                    readings.append(
                        SensorReading(
                            hive_id=hive.hive_id,
                            region_id=hive.region_id,
                            colony_side=side,
                            device_uid=hive.device_uid,
                            timestamp=timestamp,
                            observed_at=observed_at,
                            weight_kg=weight,
                            internal_temp_f=temp,
                            internal_humidity_pct=humidity,
                            external_temp_f=sensor_data.get("tE"),
                            external_humidity_pct=sensor_data.get("hE"),
                        )
                    )

    return sorted(readings, key=lambda reading: (reading.hive_id, reading.colony_side, reading.timestamp))


def load_weather_readings(data_dir: Path, hives: dict[str, HiveConfig]) -> list[WeatherReading]:
    readings: list[WeatherReading] = []

    for hive in hives.values():
        path = data_dir / f"{hive.hive_id}_data.csv"
        if not path.exists():
            continue

        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                readings.append(
                    WeatherReading(
                        hive_id=row["hive_id"],
                        observed_date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                        clock_time=row["clock_time"],
                        temperature_f=_optional_float(row.get("temperature_F")),
                        pressure_hpa=_optional_float(row.get("pressure_hPa")),
                        cloudiness_pct=_optional_float(row.get("cloudiness_percent")),
                        humidity_pct=_optional_float(row.get("humidity_percent")),
                        weather_code=_optional_int(row.get("weather_condition_code")),
                        overview=row.get("weather_overview", ""),
                    )
                )

    return readings


def _coordinate_region_ids(hive_rows: dict[str, dict[str, object]], radius_miles: float) -> dict[str, str]:
    hive_ids = sorted(hive_rows)
    adjacency: dict[str, set[str]] = {hive_id: set() for hive_id in hive_ids}

    for index, hive_id in enumerate(hive_ids):
        for other_hive_id in hive_ids[index + 1 :]:
            distance = _haversine_miles(
                float(hive_rows[hive_id]["latitude"]),
                float(hive_rows[hive_id]["longitude"]),
                float(hive_rows[other_hive_id]["latitude"]),
                float(hive_rows[other_hive_id]["longitude"]),
            )
            if distance <= radius_miles:
                adjacency[hive_id].add(other_hive_id)
                adjacency[other_hive_id].add(hive_id)

    region_ids: dict[str, str] = {}
    visited: set[str] = set()
    region_index = 1
    for hive_id in hive_ids:
        if hive_id in visited:
            continue
        component = sorted(_connected_component(hive_id, adjacency))
        visited.update(component)
        region_id = f"geo_region_{region_index:02d}"
        region_index += 1
        for member in component:
            region_ids[member] = region_id

    return region_ids


def _connected_component(start_hive_id: str, adjacency: dict[str, set[str]]) -> set[str]:
    stack = [start_hive_id]
    component: set[str] = set()

    while stack:
        hive_id = stack.pop()
        if hive_id in component:
            continue
        component.add(hive_id)
        stack.extend(sorted(adjacency[hive_id] - component, reverse=True))

    return component


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)
    haversine = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * atan2(sqrt(haversine), sqrt(1 - haversine))


def _parse_dynamodb_attribute_json(raw: str) -> dict[str, float]:
    parsed = json.loads(raw)
    values: dict[str, float] = {}
    for key, value in parsed.items():
        if isinstance(value, dict) and "N" in value:
            values[key] = float(value["N"])
    return values


def _optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
