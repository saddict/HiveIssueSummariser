# BeeMon Scoring Project Handoff

Last updated: 2026-06-23

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

`hive_config.py` currently includes these coordinate-defined sites with `REGION_RADIUS_MILES = 10`:

- `DR_WLKS` with device UID `351077454554331` in computed region `geo_region_02`
- `6LR` with device UID `868032061578211` in computed region `geo_region_01`
- `PRT_1` with device UID `868032061432054` in computed region `geo_region_01`
- `WTG_HSCHL` with device UID `868032061545061` in computed region `geo_region_01`

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
run_scoring.py                       Runs region-aware colony scoring from local cached data.
refresh_and_score.py                 One-command pipeline: optional fetch, JSON output, text report.
fetch_dynamodb.py                    Pulls sensor rows from DynamoDB into local_data/dynamodb/.
fetch_openmeteo.py                   Pulls weather rows from Open-Meteo into local_data/openmeteo/.
beemon_scoring/data_loader.py        Loads config, sensor CSVs, weather CSVs, assigns coordinate regions.
beemon_scoring/models.py             Dataclasses used by the scorer.
beemon_scoring/metrics.py            Typed Metric catalog (the 9 scoring metrics) and the shared badness-to-score scale.
beemon_scoring/quality.py            Data-quality bounds/jump thresholds and sensor-reading filtering.
beemon_scoring/weather.py            Weather-day classification (favorable/poor/neutral) and named weather thresholds.
beemon_scoring/features.py           Colony feature engineering (weight trend, temperature, humidity, weather-adjusted weight).
beemon_scoring/scoring.py            Orchestrates build_scores(); peer/z-score scoring and status assignment.
beemon_scoring/reporting.py          Regional highlight summaries plus text/JSON report rendering.
beemon_scoring/sister_comparison.py  Sister-colony comparison logic.
run_sister_comparisons.py            Prints/writes L-vs-R same-site comparison output.
README.md                            User-facing explanation and commands.
PROJECT_HANDOFF.md                   This handoff.
```

See section 15 for the 2026-06-23 refactor that split the old monolithic `scoring.py` into `metrics.py`/`quality.py`/`weather.py`/`features.py`/`scoring.py`.

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

1. Load sites from `hive_config.py` and assign coordinate-based regions using a 10-mile radius.
2. Load cached DynamoDB sensor CSVs from `local_data/dynamodb/`.
3. Load cached Open-Meteo CSVs from `local_data/openmeteo/`.
4. Keep only readings inside the rolling time window, usually 7 days.
5. Run data quality checks.
6. Drop impossible colony readings and likely sudden sensor artifacts.
7. Keep external sensor anomalies as quality notes, but do not remove colony readings for those alone.
8. Build colony-level features.
9. Classify each site-day's weather as `favorable`, `poor`, or `neutral`.
10. Calculate weather-aware daily weight features.
11. Compare each colony to eligible peers in the same configured region.
12. Produce regional highlight output plus ranked per-region colony output in the text report and `output/scoring.json`.
13. Write `output/scoring.json` with top-level `metadata`, `regions`, and `colonies` sections.

## 7. Data Quality Checks

The scorer checks colony readings before scoring.

A colony reading is excluded if any of these are impossible:

```text
weight < 0.45 kg or weight > 136.08 kg
internal temperature < 32 F or > 120 F
internal humidity < 0% or > 100%
```

A colony reading is also excluded if it makes a sudden short-interval jump compared with the previous kept reading:

```text
weight jump > 3.63 kg and > 12% within 6 hours
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
30%  current colony weight
17%  7-day weight percent change
9%   weight percent trend
6%   favorable-weather weight percent trend
4%   poor-weather weight loss
13%  temperature instability
10%  possible brood-temperature variation
6%   high-humidity exposure
5%   humidity instability
```

The weights add up to 100%.

### Current Colony Weight

This is the latest valid colony weight in kilograms. It is treated as a current strength signal: a heavier colony is generally doing better right now than a much lighter sister colony or regional peer.

This metric does not replace weight loss metrics. Instead, it gives the scorer a baseline for how strong the colony is now, while percent change and trend explain whether that strength is improving or declining.

### Weight Percent Change

This is total percent gain/loss over the window:

```text
((last_weight - first_weight) / first_weight) * 100
```

This is used instead of absolute weight loss because a 2.27 kg loss is much more severe for a 9.07 kg colony than for a 22.68 kg colony.

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

With cached data from 2026-06-16 to 2026-06-23, the coordinate-based 10-mile region assignment currently produces:

```text
geo_region_01 = 6LR, PRT_1, WTG_HSCHL
geo_region_02 = DR_WLKS
```

The text and JSON regional highlights currently show:

```text
geo_region_01
  Performing well: 6LR:L, 6LR:R
  Underperforming: WTG_HSCHL:R, WTG_HSCHL:L
  Watch: PRT_1:R

geo_region_02
  Performing well: DR_WLKS:L
  Underperforming: DR_WLKS:R
```

Top concerns by computed region are:

```text
geo_region_01: WTG_HSCHL:R - underperforming
Main drivers: favorable-weather weight percent trend, temperature instability, possible brood-temperature variation.

geo_region_02: DR_WLKS:R - underperforming
Main drivers: current colony weight, 7-day weight percent change, humidity instability.
```

There are also `PRT_1:R` excluded readings caused by large short-interval weight jumps, and those are surfaced as data-quality notes instead of silently ignored.

`output/scoring.json` now contains three top-level sections:

```text
metadata  = window, dataset summary, and region-assignment metadata
regions   = per-region site membership, highlight summaries, counts, and strongest/weakest colony lists
colonies  = full colony-level scores and metric comparisons
```

The same cached sister-colony output now marks `6LR:R` as notably weaker than `6LR:L`. The main reasons are current colony weight and humidity instability: `6LR:L` is heavier at `48.71 kg` versus `37.27 kg` for `6LR:R`, and the right side is also less humidity-stable. In the latest cache window, the right colony is the clearer same-site concern at 6LR.

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
5. Current colony weight is one of those metrics, so a much lighter side is treated as weaker even if its recent percentage drop is smaller.
6. The text report separates `Current condition` from `Trend concern`, so the overall weaker side and the side with meaningfully negative weight movement can both be shown clearly.
7. It scales the raw L-vs-R difference by the regional metric standard deviation, so tiny differences do not dominate.
8. It produces one sister score for L and one for R.
9. The side with the larger sister score is labeled mildly or notably weaker.

The separate output file is:

```text
output/sister_comparisons.json
```

Important rule:

```text
Do not mix sister-colony scoring with regional peer scoring. They answer different questions and should stay separate in the UI/API.
```

## 13. How To Scale To Many Sites And Regions

The code now assigns regions automatically from site coordinates. The current rule is:

1. Read each site's latitude and longitude from `hive_config.py`.
2. Connect two sites when their haversine distance is less than or equal to `REGION_RADIUS_MILES` miles.
3. Treat each connected component of that graph as one generated region such as `geo_region_01`.

Important implementation detail:

```text
This is connected-component clustering, not strict all-pairs-within-10-miles clustering.
Overlapping 10-mile neighborhoods merge into the same region.
```

That means a future chain of nearby sites could produce one region whose end-to-end span is greater than 10 miles, even though every step in the chain is within the 10-mile threshold.

Current implementation details:

1. `beemon_scoring/data_loader.py` computes region IDs during `load_hive_config()`.
2. `HiveConfig`, `SensorReading`, `ColonyFeatures`, and `ColonyScore` all carry `region_id`.
3. `build_scores()` records `region_assignment_method` and `region_radius_miles` in metadata.
4. `beemon_scoring/reporting.py` builds top-level `regions` summaries with `site_ids`, counts, and strongest/weakest colony lists.
5. Colony peer scoring happens only within each computed region.
6. Sister-colony scoring stays separate and does not change the regional grouping logic.

### If The 10-Mile Rule Stays

Recommended next improvements:

1. Add low-peer-count confidence labels, especially for single-site regions like the current `geo_region_02`.
2. Include optional pairwise site-distance debugging output if region grouping ever needs inspection.
3. Keep `REGION_RADIUS_MILES` configurable in `hive_config.py`.

### If The 10-Mile Rule Needs Tightening Later

Possible alternatives:

1. Require every site in a region to be within 10 miles of every other site.
2. Cluster around fixed apiary centroids or named yards.
3. Use map polygons or manual override region IDs.

Any of those would change the current connected-component behavior and should be treated as a deliberate scoring-policy change.

### Future Region-Level Scoring

The code now supports colony scoring within computed regions, but it does not yet score one region against another. When that becomes necessary, the next layer should be:

1. Aggregate region weather summaries from the site-level Open-Meteo data.
2. Aggregate region colony performance summaries from the colony scores.
3. Compare regions only against other regions with similar weather stress.
4. Keep low-peer-count regions clearly labeled.

### Rules To Avoid Logical Mistakes

Keep these rules as invariants:

```text
Do not compare colonies from different computed regions unless explicit fallback logic is added.
Do not hide which sites belong to a computed region.
Do not hide low peer counts.
Do not treat missing weather-specific data as zero.
Do not score impossible sensor readings.
Do not use absolute weight loss as the main performance signal.
Do not claim brood certainty from temperature alone.
Do not compare future region-level aggregates without weather context.
```

If the system breaks any of those rules, the output can become misleading.

## 14. Recommended Next Work

1. Add low-peer-count confidence labels for computed regions such as the current single-site `geo_region_02`.
2. Decide whether connected-component interpretation of the 10-mile rule is the long-term desired behavior.
3. Rename internal `hive_id` concepts to `site_id` while keeping CSV compatibility.
4. Add region-level scoring with weather-stress bands.
5. Add inspection notes so the model can learn from queen status, brood observations, feeding, harvests, and treatments.
6. Tune data-quality thresholds with known sensor behavior and field validation.
7. Add tests for low-peer-count fallback and future region-vs-region scoring.

## 15. Code Organization Refactor (2026-06-23)

`beemon_scoring/scoring.py` used to be a single 570-line file mixing four unrelated concerns: data-quality filtering, weather-day classification, colony feature engineering, and peer/z-score scoring, with no section boundaries between them. It was split into focused modules. This was a pure clarity refactor: no scoring behavior changed. That was confirmed by diffing `output/scoring.json` and `output/sister_comparisons.json` (both JSON and text formats) byte-for-byte before and after the change, and by the unit test suite still passing with the same 7 tests.

### What moved where

```text
beemon_scoring/metrics.py    NEW. Frozen `Metric` dataclass plus the `METRICS` catalog (was an untyped `list[dict]`),
                              and `BADNESS_Z_SCORE_SCALE` (was a bare `35` duplicated in scoring.py and
                              sister_comparison.py's score/impact formulas).
beemon_scoring/quality.py    NEW. The MIN/MAX weight, temperature, and humidity bounds plus the jump thresholds,
                              and `filter_quality_issues()` (was `_filter_quality_issues` in scoring.py).
beemon_scoring/weather.py    NEW. `RAINY_WEATHER_CODES`, `weather_by_hive()`, `weather_day_types()`
                              (were `_weather_by_hive` / `_weather_day_types` in scoring.py). The day-classification
                              thresholds that used to be inline magic numbers (50, 95, 85, 55, 90, 75, 90) are now
                              named: POOR_WEATHER_LOW_TEMP_F, POOR_WEATHER_HIGH_TEMP_F, POOR_WEATHER_CLOUDINESS_PCT,
                              FAVORABLE_WEATHER_LOW_TEMP_F, FAVORABLE_WEATHER_HIGH_TEMP_F,
                              FAVORABLE_WEATHER_CLOUDINESS_PCT, FAVORABLE_WEATHER_HUMIDITY_PCT.
beemon_scoring/features.py   NEW. `BROOD_TARGET_TEMP_F`, `HIGH_HUMIDITY_PCT`, `LOW_HUMIDITY_PCT`, `build_features()`
                              (was `_build_features`), `daily_weight_pct_changes()` (was `_daily_weight_pct_changes`),
                              `stddev()` (was `_stddev`).
beemon_scoring/scoring.py    SLIMMED. Keeps only `build_scores()` (the public entrypoint; signature unchanged),
                              `_require_data_dir`, `_score_features`, `_score_region_features`,
                              `_eligible_metric_peers`, `_badness_z`, `_flags`, `_status`. Imports the moved pieces
                              from the four modules above.
```

Naming convention used throughout the split: a function dropped its leading underscore only if something outside its new home file calls it (`build_features`, `weather_by_hive`, `weather_day_types`, `filter_quality_issues`, `stddev`, `daily_weight_pct_changes`). A helper that's only used within its own new module stayed private (`_classify_weather_day`, `_linear_slope_per_day`, `_average_optional`, and so on).

### Other clarity fixes in the same pass

- `METRICS` is now `list[Metric]` instead of `list[dict]`. Every `metric["x"]` / `metric.get("x")` access became `metric.x` attribute access in both `scoring.py` and `sister_comparison.py`, removing several no-op `int(...)`/`str(...)` casts that existed only to coerce values pulled out of an untyped dict.
- The magic scaling constant `35` — which turns a weighted-average badness z-score into a 0-100 score (a weighted-average badness z-score of 1.0, one std dev worse than peers, scales to 35 points, so ~2.86 std devs worse maxes the score out) — is now the named, commented `BADNESS_Z_SCORE_SCALE = 35.0` in `metrics.py`, imported by both `scoring.py` and `sister_comparison.py` instead of being a bare literal duplicated in each file.
- `sister_comparison.py`'s "current condition vs. trend concern" narrative logic was hard to trace because of near-synonym names: a per-metric `worse_side`, an overall `weaker_side`, and a local variable called `side` that actually meant "the side whose weight trend is declining." `_opposing_weight_trend_metrics` was renamed to `_weight_trend_concern_metrics` (it matches the "Trend concern:" label it produces in the output), and the local `side` inside `_weight_trend_sentence` was renamed to `trend_side` to stop it reading as a third, unrelated concept. A short comment above `SIGNIFICANT_WEIGHT_TREND_IMPACT` / `WEIGHT_TREND_METRICS` now explains the "weaker overall vs. trend concern" distinction once, instead of needing to be reverse-engineered from four separate functions. No `SisterMetricComparison`/`SisterSiteComparison` fields, JSON shape, or public function signatures changed.
- `tests/test_scoring_logic.py` updated its imports for the relocated functions (`daily_weight_pct_changes` now comes from `beemon_scoring.features`, `Metric` from `beemon_scoring.metrics`) and replaced a plain-dict test fixture with a real `Metric(...)` construction. No test assertions changed.

Explicitly out of scope for this pass: the 30-field `ColonyFeatures` dataclass was not broken up into nested sub-dataclasses (too many call sites for the marginal benefit), and no CLI behavior, output file paths, or JSON schema changed.
