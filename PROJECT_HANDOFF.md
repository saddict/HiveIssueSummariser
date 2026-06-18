# BeeMon Scoring Project Handoff

Last updated: 2026-06-18

This file is meant to let a future developer understand and continue the project without needing the full chat history. It explains what exists today, how the scoring works, what logic must not be broken, and how to extend the system to many sites and regions.

## 1. Mental Model

Each entry in `hive_config.py` is a physical BeeMon site/device location, not a single colony.

Each site has two colonies:

- `L` = left colony
- `R` = right colony

One DynamoDB row from a device contains readings for both colonies:

```text
wL, tL, hL = left colony weight, internal temperature, internal humidity
wR, tR, hR = right colony weight, internal temperature, internal humidity
tE, hE     = external device temperature and humidity readings
```

Open-Meteo weather is fetched once per site using that site's latitude and longitude. That same site-level weather applies to both the left and right colonies at the site.

## 2. Current Sites

`hive_config.py` currently includes:

- `DR_WLKS` with device UID `351077454554331`
- `6LR` with device UID `868032061578211`
- `PRT_1` with device UID `868032061432054`
- `WTG_HSCHL` with device UID `868032061545061`

With four sites and two colonies per site, a complete run scores eight colonies:

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

## 3. Important Files

```text
run_scoring.py                 Runs scoring from local cached data.
refresh_and_score.py           One-command pipeline: optional fetch, JSON output, text report.
fetch_dynamodb.py              Pulls sensor rows from DynamoDB into local_data/dynamodb/.
fetch_openmeteo.py             Pulls weather rows from Open-Meteo into local_data/openmeteo/.
beemon_scoring/data_loader.py  Loads config, sensor CSVs, and weather CSVs.
beemon_scoring/models.py       Dataclasses used by the scorer.
beemon_scoring/scoring.py      Data quality checks, feature extraction, peer scoring.
beemon_scoring/reporting.py    Regional text and JSON report rendering.
beemon_scoring/sister_comparison.py Sister-colony comparison logic.
run_sister_comparisons.py       Prints/writes L-vs-R same-site comparison output.
README.md                      User-facing explanation and commands.
PROJECT_HANDOFF.md             This handoff.
```

## 4. Local Data Cache

Normal scoring and tests must read local cached CSV files. They should not fetch live data every time.

Sensor cache:

```text
local_data/dynamodb/{site_id}_SENS.csv
```

Weather cache:

```text
local_data/openmeteo/{site_id}_data.csv
```

`local_data/` is the source of truth for cached scoring data. The old duplicate `Data/` folder was removed from git and is ignored by `.gitignore`.

If `local_data/dynamodb/` or `local_data/openmeteo/` is missing, `run_scoring.py` now fails clearly instead of silently falling back to stale files.

## 5. Commands

Run from cached data only:

```bash
cd /home/singhav/beemon-scoring
python3 refresh_and_score.py --skip-fetch
```

Fetch fresh DynamoDB and Open-Meteo data, then score:

```bash
python3 refresh_and_score.py
```

Run only the regional peer text report from cached data:

```bash
python3 run_scoring.py
```

Run only the sister-colony text report from cached data:

```bash
python3 run_sister_comparisons.py
```

Regenerate only regional JSON from cached data:

```bash
python3 run_scoring.py --format json --output output/scoring.json
```

Regenerate only sister-colony JSON from cached data:

```bash
python3 run_sister_comparisons.py --format json --output output/sister_comparisons.json
```

Run tests:

```bash
python3 -m unittest discover -s tests
```

## 6. Current Scoring Flow

The current scorer does this in order:

1. Load sites from `hive_config.py`.
2. Load cached DynamoDB sensor CSVs from `local_data/dynamodb/`.
3. Load cached Open-Meteo CSVs from `local_data/openmeteo/`.
4. Keep only readings inside the rolling time window, usually 7 days.
5. Run data quality checks.
6. Drop impossible colony readings and likely sudden sensor artifacts.
7. Keep external sensor anomalies as quality notes, but do not remove colony readings for those alone.
8. Build colony-level features.
9. Classify each site-day's weather as `favorable`, `poor`, or `neutral`.
10. Calculate weather-aware daily weight features.
11. Compare each colony to eligible peers.
12. Produce a ranked text report and `output/scoring.json`.

## 7. Data Quality Checks

The scorer checks colony readings before scoring.

A colony reading is excluded if any of these are impossible:

```text
weight < 1 lb or weight > 300 lb
internal temperature < 32 F or > 120 F
internal humidity < 0% or > 100%
```

A colony reading is also excluded if it makes a sudden short-interval jump compared with the previous kept reading:

```text
weight jump > 8 lb and > 12% within 6 hours
internal temperature jump > 25 F within 6 hours
internal humidity jump > 45 percentage points within 6 hours
```

External sensor values are handled differently:

```text
tE and hE anomalies are flagged, but they do not remove the colony reading.
```

Reason: the current scoring uses Open-Meteo for weather context, so bad external device weather readings should not remove otherwise useful colony readings.

## 8. Current Feature Set

The scorer compares these peer-relative metrics:

```text
26%  7-day weight percent change
16%  weight percent trend
10%  favorable-weather weight percent trend
6%   poor-weather weight loss
15%  temperature instability
13%  possible brood-temperature variation
8%   high-humidity exposure
6%   humidity instability
```

The weights add up to 100%.

### Weight Percent Change

This is total percent gain/loss over the window:

```text
((last_weight - first_weight) / first_weight) * 100
```

This is used instead of absolute weight loss because a 5 lb loss is much more severe for a 20 lb colony than for a 50 lb colony.

### Weight Percent Trend

This fits a line through all weight readings in the window and expresses the slope as percent of starting weight per day.

This catches steady decline even if the first-to-last change alone is noisy.

### Favorable-Weather Weight Percent Trend

The scorer classifies each site-day as favorable, poor, or neutral using Open-Meteo data.

For favorable days, it calculates that day's percent weight change from the first reading of the day to the last reading of the day. Then it averages those favorable-day changes.

This metric is only compared when a colony has at least one favorable daily weather window, and peers also have eligible favorable windows.

### Poor-Weather Weight Loss

For poor days, it calculates each poor day's percent weight change. It converts only losses into positive loss values and averages them.

This metric is only compared when a colony has at least one poor daily weather window, and peers also have eligible poor windows.

This avoids a previous logical issue where a poor-weather metric could accidentally span across unrelated days.

### Temperature Instability

This is the standard deviation of internal colony temperature.

```text
low standard deviation  = stable internal temperature
high standard deviation = unstable internal temperature
```

### Possible Brood-Temperature Variation

This is intentionally cautious wording. The system cannot prove brood is present or that the sensor is in the brood nest.

The scorer measures average distance from a brood-zone reference of `94.5 F`:

```text
average(abs(internal_temp - 94.5))
```

Interpretation should be:

```text
This colony is farther from the brood-zone reference than peers.
```

Not:

```text
This colony definitely has a brood problem.
```

### Humidity Features

The scorer measures:

```text
high-humidity exposure = percent of readings above 70% internal humidity
humidity instability   = standard deviation of internal humidity
```

## 9. Peer Scoring Logic

The scorer is relative. It does not say a colony is absolutely healthy or unhealthy.

For each metric:

1. Find the eligible peer colonies.
2. Calculate the peer average.
3. Calculate the peer standard deviation.
4. Calculate the colony's badness z-score.

Badness means worse than peers:

```text
positive badness = worse than peers
negative badness = better than peers
```

For metrics where higher is better, like weight percent gain, lower values are worse.

For metrics where lower is better, like instability or high humidity exposure, higher values are worse.

Weather-specific metrics only compare colonies that have enough matching weather windows. If a colony has no favorable days, it is not scored on favorable-weather trend. If it has no poor days, it is not scored on poor-weather loss.

This prevents missing weather-specific data from being treated as zero or normal.

## 10. Status Logic

The scorer produces:

```text
normal
watch
underperforming
```

A colony can become `watch` because of weaker performance signals or data-quality notes.

A colony becomes `underperforming` when:

```text
score >= 55
or it has 3 or more performance flags
```

Data-quality notes alone do not make a colony underperforming. They are surfaced so a human knows the sensor data may need inspection.

## 11. Current Known Result From Cached Data

With cached data from 2026-06-11 to 2026-06-18, the top concern is:

```text
WTG_HSCHL:R - underperforming
Main drivers: poor-weather weight loss, 7-day weight percent change, temperature instability.
```

There are also `PRT_1` external temperature anomalies, including values around `190 F`. These are reported as data-quality notes but do not remove colony readings.

## 12. Sister-Colony Same-Site Output

Regional peer scoring answers this question:

```text
How is this colony doing compared with all eligible peer colonies?
```

The sister-colony report answers a narrower same-site question:

```text
How is the left colony doing compared with the right colony at the same site?
```

Example:

```text
DR_WLKS:L vs DR_WLKS:R
```

This is useful because sister colonies share the same site-level weather and physical location. If one side is worse than the other, the difference is more likely to be colony-specific than regional weather-specific.

Implementation details:

1. `run_sister_comparisons.py` calls `build_scores()` first, so it uses the same data-quality checks and feature extraction as regional scoring.
2. `beemon_scoring/sister_comparison.py` groups the `ColonyScore` objects by site ID.
3. For each site, it expects one `L` score and one `R` score.
4. For every scoring metric, it checks which side is worse.
5. It scales the raw L-vs-R difference by the regional metric standard deviation, so tiny differences do not dominate.
6. It produces one sister score for L and one for R.
7. The side with the larger sister score is labeled mildly or notably weaker.

The separate output file is:

```text
output/sister_comparisons.json
```

Important rule:

```text
Do not mix sister-colony scoring with regional peer scoring. They answer different questions and should stay separate in the UI/API.
```

## 13. How To Scale To Many Sites And Regions

This is the recommended implementation plan for moving from one peer group to region-aware scoring.

### Step 1: Add Region Metadata

Each site needs enough metadata to know which peer group it belongs to.

Start by extending each site config with:

```python
"region_id": "boone_nc",
"apiary_id": "yard_01",
"timezone": "America/New_York",
"site_type": "production",  # optional
```

At minimum, each site needs:

```text
site_id
region_id
device_uid
latitude
longitude
timezone
```

If a site does not have a region yet, assign it to `unassigned` and do not include it in production scoring until it is placed.

### Step 2: Decide How Regions Are Defined

Use manual regions first. They are easier to reason about than automatic clustering.

Example:

```text
boone_nc
wilkes_nc
ashe_nc
```

A region should mean:

```text
Sites close enough to share broadly similar weather, forage, elevation, and management context.
```

Later, regions can be generated automatically by distance or map polygons, but manual assignment is safer for the MVP.

### Step 3: Keep Site Weather Separate

Even inside one region, each site should keep its own Open-Meteo weather because elevation and microclimate can differ.

Do not fetch one weather file for the whole region at first.

Keep:

```text
local_data/openmeteo/{site_id}_data.csv
```

Then aggregate site weather into region weather after loading.

### Step 4: Build Colony Features Per Site

Feature extraction should stay mostly the same:

```text
site -> left colony features
site -> right colony features
```

Each colony feature should carry:

```text
site_id
region_id
colony_side
weather summary for that site
quality flags
all scoring features
```

### Step 5: Score Colonies Within Their Region

Instead of comparing every colony to every other colony globally, group colonies by `region_id`.

Pseudo-logic:

```python
features_by_region = group_by(features, key="region_id")

for region_id, region_features in features_by_region.items():
    scores = score_features(region_features)
```

This means:

```text
A Boone colony is compared against Boone peer colonies.
A Wilkes colony is compared against Wilkes peer colonies.
```

Do not compare colonies across regions at this stage unless the region has too few peers.

### Step 6: Require Enough Regional Peers

A peer score is weak if there are too few peers.

Use a minimum such as:

```text
minimum 6 colonies per region
minimum 3 sites per region
```

If a region has fewer than that, mark confidence as low:

```text
region_peer_confidence = low
```

For low-confidence regions, either:

```text
show score but warn that peer group is small
or fall back to nearest neighboring region
or fall back to global scoring with a low-confidence label
```

Do not silently mix regions without telling the user.

### Step 7: Add Region-Level Weather Summaries

For each region and scoring window, aggregate the site weather:

```text
average temperature
average humidity
percent rainy windows
percent favorable days
percent poor days
cloudiness average
weather-code distribution
```

Store these as region weather features.

Example structure:

```json
{
  "region_id": "boone_nc",
  "avg_temp_f": 68.4,
  "rainy_window_pct": 12.5,
  "favorable_day_pct": 42.8,
  "poor_day_pct": 21.4
}
```

### Step 8: Score Regions Against Other Regions

After colony scoring, build region features from the colony scores.

Useful region features:

```text
average colony underperformance score
percent colonies marked underperforming
percent colonies marked watch
average 7-day weight percent change
average favorable-weather weight percent trend
average poor-weather weight loss
average data-quality issue count per site
```

Then compare regions to other regions using the same peer-scoring idea:

```text
How much worse is this region than other regions?
```

But region scoring must account for weather. A region with a bad weather week should not be punished the same way as a region with perfect weather.

### Step 9: Weather-Normalize Region Scores

Start with simple normalization.

For each region, calculate weather stress:

```text
weather_stress = poor_day_pct + rainy_window_pct + extreme_temp_pct
```

Then compare region performance against regions with similar weather stress.

Simple MVP approach:

```text
low weather stress
medium weather stress
high weather stress
```

Only compare regions inside the same stress band.

Example:

```text
Boone had high rain and cool weather.
Wilkes had high rain and cool weather.
Compare Boone against Wilkes.
Do not directly compare Boone against a sunny warm region without noting the difference.
```

### Step 10: Region Output Should Explain Both Levels

A final report should have two levels.

Colony-level example:

```text
WTG_HSCHL:R is underperforming within region boone_nc.
It lost 7.3% weight while the regional peer average was 1.9% loss.
It also showed higher temperature instability than regional peers.
```

Region-level example:

```text
Region boone_nc is underperforming relative to regions with similar weather stress.
35% of colonies were watch or underperforming, compared with 18% in similar-weather regions.
The main regional driver was poor favorable-weather weight trend.
```

### Step 11: Suggested Code Structure For Regions

Do not rewrite everything at once. Add region support in small pieces.

Recommended changes:

1. Rename `HiveConfig` to `SiteConfig` or add `SiteConfig` while keeping CSV fields compatible.
2. Add `region_id` to the config loader.
3. Add `region_id` to `SensorReading`, `WeatherReading`, and `ColonyFeatures`.
4. Change `build_scores()` to group features by region before scoring.
5. Add `build_region_scores()` after colony scoring works per region.
6. Add `RegionFeatures` and `RegionScore` dataclasses.
7. Add region sections to `reporting.py`.
8. Add tests for region grouping and low-peer-count behavior.

### Step 12: Rules To Avoid Logical Mistakes

Keep these rules as invariants:

```text
Do not compare colonies from different regions unless explicitly falling back.
Do not hide low peer counts.
Do not treat missing weather-specific data as zero.
Do not score impossible sensor readings.
Do not use absolute weight loss as the main performance signal.
Do not claim brood certainty from temperature alone.
Do not compare regions without weather context.
```

If the system breaks any of those rules, the output can become misleading.

## 14. Recommended Next Work

1. Add `region_id` and `timezone` to `hive_config.py`.
2. Rename internal `hive_id` concepts to `site_id` while keeping CSV compatibility.
3. Add regional grouping and per-region colony scoring.
4. Add low-peer-count confidence labels.
5. Add region-level scoring with weather-stress bands.
6. Add inspection notes so the model can learn from queen status, brood observations, feeding, harvests, and treatments.
7. Tune data-quality thresholds with known sensor behavior and field validation.
8. Add tests for region grouping, low-peer-count fallback, and region scoring.
