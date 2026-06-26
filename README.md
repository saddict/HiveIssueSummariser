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
5. Detecting abrupt weight events (harvests, swarms, supering/feeding) and splitting the window into stable segments around them, so a beekeeper's intervention is reported as an event rather than corrupting the weight trend (see "Weight events" below).
5. Comparing each colony only against peer colonies in the same configured region.
6. Producing region highlights plus ranked colony output for each region, along with structured JSON.
7. Producing a separate sister-colony report comparing L vs R at each site.

## Weight Events (Harvests, Swarms, Supering)

Colony weight does not always move smoothly. A honey harvest removes several
kilograms in an hour; a swarm departs with a large mass of bees; adding a super
or feeder steps the weight up. If the scorer fit a single trend straight across
one of these steps, it would report a thriving colony as collapsing (harvest) or
a declining one as booming (supering), and the event itself would never be
surfaced. Earlier versions also risked discarding a real harvest as a "sudden
jump" data-quality fault, hiding it entirely.

`beemon_scoring/events.py` detects these step changes and splits the window into
*segments* — the stretches of ordinary day-to-day behaviour between events. The
feature builder then:

- measures weight trend (`weight_slope_*`) as a span-weighted average of each
  segment's own slope, so a long stable stretch counts for more than a short
  one and the step itself is excluded;
- measures net change (`weight_delta_kg`, `weight_pct_change`) as the sum of the
  within-segment changes, which is the colony's organic gain or loss with the
  intervention removed;
- skips event days when computing the poor-/favorable-weather metrics; and
- reports each event as an explicit flag, e.g.
  `Likely harvest: weight dropped 12.8 kg (-26.8%) around 2026-06-23T18:00...`.

Events are classified as `harvest`, `swarm` (a drop paired with a sharp
internal-climate change), or `addition` (a sustained gain). The labels are
deliberately probabilistic — sensors cannot confirm the cause — so they are
always phrased as "Likely ...". A normal week contains no events and reduces to
a single segment, leaving the original behaviour unchanged. Detection is
conservative: only a sharp, sustained level shift that survives a short
confirmation window counts, so ordinary foraging gains and overnight respiration
loss stay within one segment.

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

`local_data/` is the source of truth for cached scoring data. If those folders are missing, the scorer fails with a clear error instead of silently using stale files.

## How Data Is Refreshed

Use this when you want fresh live data from DynamoDB and Open-Meteo:

```bash
python3 refresh_and_score.py
```

That command runs the full live pipeline:

1. `fetch_dynamodb.py` pulls the latest sensor readings from DynamoDB.
2. `fetch_openmeteo.py` pulls the latest weather rows from Open-Meteo.
3. `run_scoring.py` regenerates `output/scoring.json`.
4. `run_sister_comparisons.py` regenerates `output/sister_comparisons.json`.
5. `run_scoring.py` prints the regional peer report.
6. `run_sister_comparisons.py` prints the same-site sister-colony report.

By default, the fetch scripts write into `local_data/`, so future offline runs use the latest cached data.

## How To Run Without Fetching

Use this for tests, demos, and repeated local scoring runs:

```bash
python3 refresh_and_score.py --skip-fetch
```

That command does not call DynamoDB or Open-Meteo. It only reads the cached CSVs, regenerates JSON, and prints the report.

You can also run just the regional peer text report:

```bash
python3 run_scoring.py
```

Or regenerate only regional JSON:

```bash
python3 run_scoring.py --format json --output output/scoring.json
```

Run only the sister-colony text report:

```bash
python3 run_sister_comparisons.py
```

Regenerate only sister-colony JSON:

```bash
python3 run_sister_comparisons.py --format json --output output/sister_comparisons.json
```

Use a different scoring window:

```bash
python3 run_scoring.py --window-days 14
python3 run_scoring.py --window-days 30
python3 run_sister_comparisons.py --window-days 30
```

`--window-days` only changes how much of the cached data is scored. If the local cache doesn't already hold that much history, fetch it first:

```bash
python3 fetch_dynamodb.py --days 30
python3 fetch_openmeteo.py --days 30
```

Run the unit tests:

```bash
python3 -m unittest discover -s tests
```

## DynamoDB Fetching

`fetch_dynamodb.py` uses the site device UIDs from `hive_config.py` and queries the configured BeeMon sensor table.

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

The scorer compares colonies, not sites. Regions are assigned automatically from site coordinates in two steps. First, sites within `REGION_RADIUS_MILES` of each other are connected into components, one component per region. Second, any region with fewer than `MIN_REGION_SITE_COUNT` sites is merged into its nearest neighboring region by distance, even if that distance exceeds `REGION_RADIUS_MILES`, repeating until every region meets that floor or only one region remains. This second step exists because comparing a colony's metrics against too few peers produces statistically meaningless results: with only one other peer, a z-score is always exactly +-1 standard deviation regardless of how big or small the real gap is, since it carries no information beyond which side is higher (see `BADNESS_Z_SCORE_SCALE` in `beemon_scoring/metrics.py` for the math). A site's own L/R colonies are excluded from counting toward this floor, since comparing a colony only against its own sister is already the dedicated job of the [sister-colony report](#sister-colony-comparison) below.

With the current `10` mile radius and a `2`-site floor, `DR_WLKS` sits alone past the radius (its nearest neighbor, `6LR`, is about 12.6 miles away) but is still merged into the `6LR, PRT_1, WTG_HSCHL` cluster to keep its peer comparisons meaningful, so the live grouping is one region, `geo_region_01 = 6LR, DR_WLKS, PRT_1, WTG_HSCHL`, and a complete run scores eight colonies:

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

- current colony weight from the latest valid reading
- total percent weight change over the window
- linear percent weight trend per day
- average favorable-day percent weight change
- average poor-day percent weight loss
- absolute kilogram change for display context only

Current weight is a primary strength signal. A heavier colony is generally doing better right now than a much lighter sister colony or regional peer, while the percent-change and trend metrics explain whether that strength is improving or declining.

A colony losing a larger percentage of its starting weight than peers during the same regional window is still treated as more concerning, especially when current weight is also weak.

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

### Data Quality Checks

Before feature extraction, the scorer checks for impossible colony sensor values and sudden jumps. Colony readings are excluded from scoring when weight, internal temperature, or internal humidity is outside plausible bounds. Sudden short-interval jumps in weight, internal temperature, or internal humidity are also excluded as likely sensor artifacts.

External device readings such as `tE` and `hE` are flagged when implausible, but they do not remove colony readings because the current scoring uses Open-Meteo for weather context. The report shows valid reading counts, excluded reading counts, and data-quality notes.

### Weather-Adjusted Weight Features

Weather is used as site-level context and now contributes directly to weight scoring. Each site-day is classified as `favorable`, `poor`, or `neutral` using Open-Meteo temperature, cloud cover, humidity, and weather code.

Favorable days are mild, not rainy, not overly cloudy, and not extremely humid. Poor days include rainy conditions, extreme temperatures, or very heavy cloud cover.

For each matching weather day, the scorer compares the first and last colony weights from that day. It then averages those daily changes and adds two peer-relative metrics:

- favorable-weather weight percent trend: average daily percent weight change on favorable days
- poor-weather weight loss: average daily percent weight loss on poor days

The report still includes weather averages such as average outside temperature and rainy-reading percentage.

## Sister-Colony Comparison

The sister-colony report compares only the left and right colonies on the same physical site. For example, `DR_WLKS:L` is compared directly with `DR_WLKS:R`.

This output answers a different question from regional peer scoring:

```text
Is one side of this same site weaker than its sister colony?
```

For each metric, it checks which side is worse. The raw L-vs-R difference is scaled by the regional metric spread so tiny differences do not dominate the report. Current colony weight is included as a primary same-site strength signal, so a much lighter sister colony is treated as weaker even if its recent percentage drop is smaller. The text report separates this into `Current condition` and `Trend concern`: current condition names the side that is weaker overall, while trend concern names any sister colony with meaningfully negative weight movement. The result is a separate sister score for L and R at each site.

This report is useful because both colonies at the same site share the same device location and site-level weather, so differences between them can highlight colony-specific issues that regional scoring may hide.

## Peer Comparison

The system is relative. It does not say a colony is absolutely good or bad.

For each metric, it calculates a peer average and peer standard deviation across all colonies. Then it computes a signed badness z-score:

```text
badness z-score = how much worse this colony is than the peer average
```

For metrics where higher is better, such as current colony weight or weight percent change, lower values are worse.

For metrics where lower is better, such as instability or high-humidity exposure, higher values are worse.

Positive badness means the colony is worse than peers. Larger positive values are more concerning.

## Overall Status

The scoring engine combines weighted metric badness into an underperformance score from `0` to `100`.

Current weighted drivers:

```text
30%  current colony weight
17%  weight percent change
9%   weight percent trend
6%   favorable-weather weight percent trend
4%   poor-weather weight loss
13%  temperature instability
10%  possible brood-temperature variation
6%   high-humidity exposure
5%   humidity instability
```

Statuses are:

```text
normal          = no major peer-relative concern
watch           = one or more weaker signals
underperforming = stronger or multiple concerning signals
```

## Outputs

Regional text report:

```bash
python3 run_scoring.py
```

JSON reports:

```text
output/scoring.json
output/sister_comparisons.json
```

The text report gives:

- scoring window
- number of regions and colonies compared
- sensor coverage
- regional highlights for strong and weak colonies
- ranked colony lists within each region
- top metric drivers
- explicit flags

The JSON output contains `metadata`, `regions`, and `colonies` sections for use by a future dashboard, API, or LLM explanation layer.

## Current Limitations

This MVP is intentionally explainable and conservative, but it has limits:

- It does not diagnose disease, queenlessness, mite pressure, or brood status.
- It cannot know whether the temperature sensor is exactly in the brood nest.
- Region assignment uses connected components of the `REGION_RADIUS_MILES` distance graph, so long chains of nearby sites can create a region whose end-to-end span is greater than 10 miles.
- The `MIN_REGION_SITE_COUNT` merge step can pull an isolated site into a region well beyond `REGION_RADIUS_MILES` (DR_WLKS today, at ~12.6 miles from its nearest neighbor). This trades strict geographic locality for a peer-comparison group large enough that z-scores carry real magnitude information instead of always landing at exactly +-1 standard deviation.
- Data-quality thresholds are conservative heuristics and should be tuned with field validation.
- Weather adjustment is still simple day-level logic, not a full nectar-flow or forage model.

The output should be treated as inspection guidance, not a final biological diagnosis.

## Recommended Next Work

1. Tune data-quality thresholds with known sensor behavior and field validation.
2. Improve weather adjustment with richer forage and nectar-flow signals.
3. Add tests around CSV parsing, feature extraction, quality filtering, weather classification, and score ranking.
4. Rename internal code concepts from `hive_id` to `site_id` while preserving CSV compatibility.
5. Add inspection notes so the system can learn from queen status, brood observations, feeding, harvests, and treatments.
