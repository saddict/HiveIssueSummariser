from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from beemon_scoring.data_loader import DEFAULT_TIMEZONE, load_hive_config


API_URL = "https://api.open-meteo.com/v1/forecast"
CSV_FIELDS = [
    "hive_id",
    "latitude",
    "longitude",
    "date",
    "clock_time",
    "temperature_F",
    "pressure_hPa",
    "cloudiness_percent",
    "humidity_percent",
    "weather_condition_code",
    "weather_overview",
]
TARGET_HOURS = (7, 14, 19)

WMO_DESCRIPTIONS = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch recent Open-Meteo weather rows for configured BeeMon sites.")
    parser.add_argument("--days", type=int, default=7, help="Number of recent days to fetch, including today.")
    parser.add_argument("--end", default=None, help="End date as YYYY-MM-DD. Defaults to today in America/New_York.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Timezone used by Open-Meteo and output dates.")
    parser.add_argument("--output-dir", type=Path, default=Path("local_data/openmeteo"), help="Directory for cached Open-Meteo weather CSVs.")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    sites, _, _ = load_hive_config(project_root / "hive_config.py")
    timezone = ZoneInfo(args.timezone)
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else datetime.now(timezone).date()
    start_date = end_date - timedelta(days=args.days - 1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for site in sites.values():
        hourly = fetch_hourly_weather(site.latitude, site.longitude, start_date.isoformat(), end_date.isoformat(), args.timezone)
        rows = build_rows(site, hourly)
        output_path = args.output_dir / f"{site.hive_id}_data.csv"
        write_rows(output_path, rows)
        total += len(rows)
        print(f"{site.hive_id}: wrote {len(rows)} weather rows for {start_date} to {end_date} -> {output_path}")

    print(f"Done. Wrote {total} Open-Meteo rows across {len(sites)} sites.")


def fetch_hourly_weather(latitude: float, longitude: float, start_date: str, end_date: str, timezone: str) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": "temperature_2m,relative_humidity_2m,surface_pressure,cloud_cover,weather_code",
        "temperature_unit": "fahrenheit",
        "timezone": timezone,
    }
    url = f"{API_URL}?{urlencode(params)}"
    with urlopen(url, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Open-Meteo returned HTTP {response.status} for {url}")
        return json.loads(response.read().decode("utf-8"))["hourly"]


def build_rows(site, hourly: dict) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    times = hourly["time"]
    temperatures = hourly["temperature_2m"]
    humidities = hourly["relative_humidity_2m"]
    pressures = hourly["surface_pressure"]
    cloud_cover = hourly["cloud_cover"]
    weather_codes = hourly["weather_code"]

    for index, time_value in enumerate(times):
        observed_at = datetime.fromisoformat(time_value)
        if observed_at.hour not in TARGET_HOURS:
            continue
        temperature = temperatures[index]
        humidity = humidities[index]
        clouds = cloud_cover[index]
        code = int(weather_codes[index])
        rows.append(
            {
                "hive_id": site.hive_id,
                "latitude": site.latitude,
                "longitude": site.longitude,
                "date": observed_at.date().isoformat(),
                "clock_time": observed_at.strftime("%H:%M"),
                "temperature_F": round(temperature, 1),
                "pressure_hPa": round(pressures[index], 1),
                "cloudiness_percent": int(round(clouds)),
                "humidity_percent": int(round(humidity)),
                "weather_condition_code": code,
                "weather_overview": describe_weather(observed_at.hour, temperature, humidity, clouds, code),
            }
        )
    return rows


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def describe_weather(hour: int, temperature_f: float, humidity_pct: float, cloudiness_pct: float, code: int) -> str:
    daypart = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    condition = WMO_DESCRIPTIONS.get(code, f"weather code {code}")
    return (
        f"{temperature_phrase(temperature_f)} and {humidity_phrase(humidity_pct)} {daypart} "
        f"with {cloud_phrase(cloudiness_pct)} skies and {condition} conditions."
    )


def temperature_phrase(value: float) -> str:
    if value < 32:
        return "Freezing"
    if value < 50:
        return "Cool"
    if value < 70:
        return "Mild"
    if value < 85:
        return "Warm"
    return "Hot"


def humidity_phrase(value: float) -> str:
    if value < 40:
        return "dry"
    if value < 65:
        return "moderately humid"
    return "humid"


def cloud_phrase(value: float) -> str:
    if value < 20:
        return "mostly clear"
    if value < 60:
        return "partly cloudy"
    return "mostly cloudy"


if __name__ == "__main__":
    main()
