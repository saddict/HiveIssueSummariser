# BeeMon Scoring MVP

This MVP scores each colony side (`L` and `R`) relative to nearby peers using cached local CSV data.

Each entry in `hive_config.py` is a site/device location. Each site has two colonies:

- `L` = left colony
- `R` = right colony

Open-Meteo weather is site-level and applies to both colonies at that site.

## Local Data Cache

Scoring reads local cached files by default:

```text
local_data/dynamodb/{site_id}_SENS.csv
local_data/openmeteo/{site_id}_data.csv
```

This lets tests and scoring runs work without fetching from DynamoDB or Open-Meteo every time.

## Run Everything

Refresh live data, regenerate JSON, and print the score report:

```bash
python3 refresh_and_score.py
```

Run the same pipeline without fetching live data:

```bash
python3 refresh_and_score.py --skip-fetch
```

## Run Offline Scoring

```bash
python3 run_scoring.py
```

Generate JSON:

```bash
python3 run_scoring.py --format json --output output/scoring.json
```

Use a different rolling window:

```bash
python3 run_scoring.py --window-days 14
```

## Refresh Local Cache

Only run these when you intentionally want fresh data from the network:

```bash
python3 fetch_dynamodb.py
python3 fetch_openmeteo.py
```

The fetch scripts write into `local_data/` by default.

## Inputs

The sensor parser expects DynamoDB AttributeValue JSON in `sensor_data`, including:

- `wL`, `tL`, `hL`
- `wR`, `tR`, `hR`
- optional external readings `tE`, `hE`

Weather CSV fields are:

```text
hive_id,latitude,longitude,date,clock_time,temperature_F,pressure_hPa,cloudiness_percent,humidity_percent,weather_condition_code,weather_overview
```

## Scoring

The scoring engine compares colony sides in the region over the configured rolling window. Positive badness means a colony is worse than peers.

Current weighted drivers:

- 7-day weight change
- weight trend
- internal temperature instability
- possible brood-temperature variation from 94.5 F
- high-humidity exposure
- humidity instability

The text report names the most concerning colony, explains the main drivers, and ranks all colonies.

## Notes

This is intentionally explainable and rule/statistics based. It does not diagnose disease or queen status. It identifies relative underperformance patterns that should guide inspection.
