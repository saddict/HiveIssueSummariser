# Hive Issue Summariser

Hive Issue Summariser is an explainable MVP for finding which bee colonies are underperforming compared with nearby peers. It does not diagnose disease, queen status, or brood presence with certainty. It summarizes sensor and weather patterns so a beekeeper knows which colony is worth inspecting first and why.

## Core Concept

Each entry in `hive_config.py` represents a physical BeeMon site/device location. Each site has two colonies:

- `L` = left colony
- `R` = right colony

A site-level device reports separate readings for both colonies in the same DynamoDB row:

```text
wL, tL, hL = left colony weight, internal temperature, internal humidity
wR, tR, hR = right colony weight, internal temperature, internal humidity
tE, hE     = external device temperature and humidity readings
```

Open-Meteo data is fetched once per site using that site's latitude and longitude. That weather context is shared by both colonies at the site.

## What The System Does

The system answers this question:

```text
Which colony is performing worse than its regional peers over the same time window, and what trends make it look worse?
```

It does this by:

1. Reading cached DynamoDB sensor data for each configured site.
2. Splitting each site into left and right colony records.
3. Reading cached Open-Meteo weather data for each site.
4. Building 7-day features for each colony.
5. Comparing each colony against all other colonies in the peer group.
6. Producing a ranked natural-language report and a structured JSON file.

## Local Data Cache

Scoring uses local CSV files by default. This is intentional so tests and normal scoring runs do not call DynamoDB or Open-Meteo every time.

Sensor cache:

```text
local_data/dynamodb/{site_id}_SENS.csv
```

Weather cache:

```text
local_data/openmeteo/{site_id}_data.csv
```

Example:

```text
local_data/dynamodb/WTG_HSCHL_SENS.csv
local_data/openmeteo/WTG_HSCHL_data.csv
```

The older `Data/` folder is kept as legacy/sample data. `run_scoring.py` prefers `local_data/` and only falls back to `Data/` if the cache folders are missing.

## How Data Is Refreshed

Use this when you want fresh live data from DynamoDB and Open-Meteo:

```bash
python3 refresh_and_score.py
```

That command runs the full live pipeline:

1. `fetch_dynamodb.py` pulls the latest sensor readings from DynamoDB.
2. `fetch_openmeteo.py` pulls the latest weather rows from Open-Meteo.
3. `run_scoring.py` regenerates `output/scoring.json`.
4. `run_scoring.py` prints the human-readable report.

By default, the fetch scripts write into `local_data/`, so future offline runs use the latest cached data.

## How To Run Without Fetching

Use this for tests, demos, and repeated local scoring runs:

```bash
python3 refresh_and_score.py --skip-fetch
```

That command does not call DynamoDB or Open-Meteo. It only reads the cached CSVs, regenerates JSON, and prints the report.

You can also run just the text report:

```bash
python3 run_scoring.py
```

Or regenerate only JSON:

```bash
python3 run_scoring.py --format json --output output/scoring.json
```

Use a different scoring window:

```bash
python3 run_scoring.py --window-days 14
```

## DynamoDB Fetching

`fetch_dynamodb.py` uses the site device UIDs from `hive_config.py` and queries this table:

```text
beemon-dev-telemetry-readings
```

The query expects:

```text
device_uid = partition key, DynamoDB String
timestamp  = sort key, DynamoDB Number
```

For each site, it writes a CSV like this:

```text
local_data/dynamodb/{site_id}_SENS.csv
```

The CSV keeps the DynamoDB `sensor_data` payload as AttributeValue JSON, because the loader already knows how to parse values such as:

```json
{"wL":{"N":"28.5"},"tL":{"N":"94.8"},"hL":{"N":"55.1"}}
```

## Open-Meteo Fetching

`fetch_openmeteo.py` uses each site's latitude and longitude from `hive_config.py`.

It fetches hourly weather, then keeps three representative local times per day:

```text
07:00 morning
14:00 afternoon
19:00 evening
```

For each site, it writes:

```text
local_data/openmeteo/{site_id}_data.csv
```

Weather fields include outside temperature, pressure, cloud cover, humidity, WMO weather code, and a generated short weather summary.

## How Scoring Works

The scorer compares colonies, not sites. With four configured sites, a complete run scores eight colonies:

```text
DR_WLKS:L
DR_WLKS:R
6LR:L
6LR:R
PRT_1:L
PRT_1:R
WTG_HSCHL:L
WTG_HSCHL:R
```

For each colony, it builds features over the configured rolling window, currently 7 days by default.

### Weight Features

The scorer measures:

- total weight change over the window
- linear weight trend in pounds per day
- percent weight change

A colony losing more weight than peers during the same regional window is treated as more concerning.

### Temperature Instability

Temperature instability is measured as standard deviation of the colony's internal temperature readings.

```text
low standard deviation  = stable internal temperature
high standard deviation = unstable internal temperature
```

The system does not judge instability in isolation. It compares that value against all peer colonies. A colony is flagged only when its variation is worse than the peer group.

### Possible Brood-Temperature Variation

This is intentionally worded as possible variation, not a certain brood diagnosis.

The current MVP uses `94.5 F` as a reference point for brood-zone temperature and calculates the average absolute distance from that value:

```text
average(|internal_temp - 94.5|)
```

This does not prove brood is present, absent, healthy, or unhealthy. It only means:

```text
This colony's internal temperature is farther from the expected brood-zone reference than its peers.
```

The model stays cautious because sensor placement, colony state, queen status, and actual brood presence are not known from this data alone.

### Humidity Features

The scorer measures:

- average internal humidity
- humidity standard deviation
- percent of readings above 70% internal humidity
- percent of readings below 40% internal humidity

High-humidity exposure and unstable humidity are treated as concerning only when they are worse than peers.

### Weather Context

Weather is used as site-level context. The report includes weather averages such as average outside temperature and rainy-reading percentage.

The current MVP does not yet fully weather-adjust every scoring metric. It mainly uses weather to make the natural-language report more informative and to ensure each site has matching regional context. A recommended next step is to add stronger weather-adjusted features, such as comparing weight gain during favorable vs rainy periods.

## Peer Comparison

The system is relative. It does not say a colony is absolutely good or bad.

For each metric, it calculates a peer average and peer standard deviation across all colonies. Then it computes a signed badness z-score:

```text
badness z-score = how much worse this colony is than the peer average
```

For metrics where higher is better, such as weight gain, lower values are worse.

For metrics where lower is better, such as instability or high-humidity exposure, higher values are worse.

Positive badness means the colony is worse than peers. Larger positive values are more concerning.

## Overall Status

The scoring engine combines weighted metric badness into an underperformance score from `0` to `100`.

Current weighted drivers:

```text
34%  7-day weight change
22%  weight trend
16%  temperature instability
14%  possible brood-temperature variation
8%   high-humidity exposure
6%   humidity instability
```

Statuses are:

```text
normal          = no major peer-relative concern
watch           = one or more weaker signals
underperforming = stronger or multiple concerning signals
```

## Outputs

Text report:

```bash
python3 run_scoring.py
```

JSON report:

```text
output/scoring.json
```

The text report gives:

- scoring window
- number of colonies compared
- sensor coverage
- most concerning colony
- ranked colony list
- top metric drivers
- explicit flags

The JSON output contains the same scoring result in structured form for use by a future dashboard, API, or LLM explanation layer.

## Current Limitations

This MVP is intentionally explainable and conservative, but it has limits:

- It does not diagnose disease, queenlessness, mite pressure, or brood status.
- It cannot know whether the temperature sensor is exactly in the brood nest.
- It treats all configured colonies as one peer group.
- It does not yet filter impossible sensor values or sudden sensor jumps.
- It does not yet deeply adjust every metric by weather conditions.

The output should be treated as inspection guidance, not a final biological diagnosis.

## Recommended Next Work

1. Add data quality checks for impossible sensor values and sudden jumps.
2. Add weather-adjusted scoring features for favorable vs poor foraging windows.
3. Add tests around CSV parsing, feature extraction, and score ranking.
4. Rename internal code concepts from `hive_id` to `site_id` while preserving CSV compatibility.
5. Add inspection notes so the system can learn from queen status, brood observations, feeding, harvests, and treatments.
