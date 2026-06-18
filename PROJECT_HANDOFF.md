# BeeMon Scoring Project Handoff

Last updated: 2026-06-18

## Current project model

Each entry in `hive_config.py` is a site/device location, not a single colony. Each site has two colonies:

- `L` = left colony
- `R` = right colony

The DynamoDB sensor payload contains separate left/right colony values:

- left: `wL`, `tL`, `hL`
- right: `wR`, `tR`, `hR`
- external device readings: `tE`, `hE`

Open-Meteo weather is site-level and applies to both colonies at that site.

## Configured sites as of this handoff

`hive_config.py` currently includes:

- `DR_WLKS` with device UID `351077454554331`
- `6LR` with device UID `868032061578211`
- `PRT_1` with device UID `868032061432054`
- `WTG_HSCHL` with device UID `868032061545061`

## Local data cache

Scoring and tests should use local cached CSV data by default. Do not fetch from DynamoDB/Open-Meteo during ordinary tests.

Sensor cache:

```text
local_data/dynamodb/{site_id}_SENS.csv
```

Weather cache:

```text
local_data/openmeteo/{site_id}_data.csv
```

`run_scoring.py` reads these folders by default. It only falls back to the legacy `Data/` folder if the cache folders are missing.

## Refreshing cache from live services

Only run these commands when intentionally refreshing local data:

```bash
cd /home/singhav/beemon-scoring
python3 fetch_dynamodb.py
python3 fetch_openmeteo.py
```

`fetch_dynamodb.py` writes `local_data/dynamodb/` by default.
`fetch_openmeteo.py` writes `local_data/openmeteo/` by default.

## DynamoDB

Table name:

```text
beemon-dev-telemetry-readings
```

The table is queried by:

- partition key: `device_uid` as DynamoDB string (`S`)
- sort key: `timestamp` as DynamoDB number (`N`)

## Open-Meteo

Weather rows are sampled at local times:

- `07:00`
- `14:00`
- `19:00`

Fields match the original provided weather CSV shape:

```text
hive_id,latitude,longitude,date,clock_time,temperature_F,pressure_hPa,cloudiness_percent,humidity_percent,weather_condition_code,weather_overview
```

## One-command pipeline

Refresh live data, regenerate JSON, and print the score report:

```bash
cd /home/singhav/beemon-scoring
python3 refresh_and_score.py
```

Run the same pipeline from cached local data only:

```bash
python3 refresh_and_score.py --skip-fetch
```

## Scoring

Run offline text report:

```bash
cd /home/singhav/beemon-scoring
python3 run_scoring.py
```

Regenerate JSON offline:

```bash
python3 run_scoring.py --format json --output output/scoring.json
```

The scorer compares colonies, not sites. With four configured sites, a complete dataset should produce eight scored colonies:

```text
DR_WLKS:L, DR_WLKS:R, 6LR:L, 6LR:R, PRT_1:L, PRT_1:R, WTG_HSCHL:L, WTG_HSCHL:R
```

Current weighted scoring drivers:

- 7-day weight percent change
- weight percent trend
- favorable-weather weight percent trend
- poor-weather weight loss
- internal temperature instability
- possible brood-temperature variation from 94.5 F
- high-humidity exposure
- humidity instability

Positive badness z-scores mean worse than regional peers. The natural-language report names the most concerning colony, lists drivers, and ranks all colonies.

## Last known run result

After fetching DynamoDB and Open-Meteo data on 2026-06-18, the scorer compared eight colonies over approximately seven days.

Top concern from that run:

```text
WTG_HSCHL:R - underperforming
Main drivers: 7-day weight percent change, temperature instability, and possible brood-temperature variation.
```

Second concern:

```text
WTG_HSCHL:L - underperforming
Main drivers: high-humidity exposure, possible brood-temperature variation, and humidity instability.
```

A raw sensor anomaly was observed in `PRT_1`: external device temperature `tE` included very high values such as `190.04 F`. The current scoring relies on Open-Meteo for weather context, so that external sensor anomaly does not drive weather normalization, but it may be worth validating later.

## Recommended next work

1. Tune data-quality thresholds with known sensor behavior and field validation.
2. Improve weather adjustment with richer forage and nectar-flow signals.
3. Add tests for parsing, quality filtering, weather classification, and scoring using `local_data/` only.
4. Consider renaming code concepts from `hive_id` to `site_id` internally while preserving CSV compatibility.
5. Add inspection notes so the system can learn from queen status, brood observations, feeding, harvests, and treatments.
