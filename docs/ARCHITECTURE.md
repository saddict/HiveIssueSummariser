# BeeMon Scoring: System Architecture, Event Detection, and Data-Quality Separation

This document explains how the scoring pipeline is put together, how abrupt
weight events (harvests, swarms, supering/feeding) are detected, and — the part
that is easiest to get wrong — how the system tells a *real hive event* apart
from *anomalous sensor data*, given that both look like sudden jumps in the
same signal.

Every tunable constant referenced below lives in `thresholds.toml` at the repo
root (see §1, Configuration); the empirical grounding for each is documented in
`docs/METHODS.md`.

---

## 1. Pipeline overview

The system is a batch pipeline: fetch → load → window → **detect events** →
**quality-filter** → feature-build → score → report. The ordering of the two
bolded stages is deliberate and is covered in §4.

```
fetch_dynamodb.py        fetch_openmeteo.py
      │                        │
      ▼                        ▼
local_data/dynamodb/*.csv  local_data/openmeteo/*.csv
      │                        │
      └────────┬───────────────┘
               ▼
     data_loader.py   ── hive_config.py  (site inventory: which hives, where)
               │       └── thresholds.toml (every tunable value, via
               │           │                beemon_scoring/thresholds.py)
               │           └─ coordinate-radius region assignment
               ▼
     scoring.prepare_features()
        1. window readings (rolling N days, anchored at newest reading)
        2. events.detect_site_events()      ← runs FIRST, on pre-filter data
        3. quality.filter_quality_issues()  ← consumes the event list
        4. weather.weather_day_types()      (favorable / poor / neutral days)
        5. features.build_features()        (per-colony feature vector,
               │                             segmented weight trends)
               ▼
     scoring._score_features()   (peer-relative badness z-scores per region)
               │
      ┌────────┴─────────┐
      ▼                  ▼
run_scoring.py     run_sister_comparisons.py
(regional ranking)  (L vs R within each site)
      ▼                  ▼
output/scoring.json  output/sister_comparisons.json
```

Entry points: `run_scoring.py` and `run_sister_comparisons.py` (score cached
data), `refresh_and_score.py` (fetch fresh data, then run both). Everything
downstream of the fetchers is pure computation over the CSV caches — no I/O in
the library modules — which is what makes the validation harness
(`validate_events.py` against `ground_truth/events.json`) and the sensitivity
spikes cheap to run.

### Configuration (`thresholds.toml`, `beemon_scoring/thresholds.py`)

Every tunable number — window size, status cuts, event floors, quality bounds
and jump limits, weather cutoffs, region radius, metric weights — lives in
`thresholds.toml` at the repo root, grouped into sections with the grounding of
each value in a comment beside it. Modules still expose their familiar constants
(`events.ADDITION_FLOOR_KG`, `quality.MAX_WEIGHT_JUMP_KG`, …) but read them from
that file at import; **no module defines a fallback default**, so a missing key
raises instead of silently substituting a number nobody wrote down. That is the
property worth protecting: one file to change, one file for a reviewer to audit,
and no second copy of a threshold to drift out of sync.

`hive_config.py` is now purely the site inventory. The settings that used to live
there moved, and `data_loader._reject_moved_settings()` raises if one is left
behind rather than letting an edit there quietly do nothing.

Two override hatches, both read at import: `BEEMON_THRESHOLDS=/path/to.toml`
swaps the whole file (a sweep, or a second deployment), and `BEEMON_<DOTTED_KEY>`
overrides a single value (`BEEMON_STATUS_WATCH_SCORE=25`), coerced to the type
used in the file. They are for experiments — anything worth keeping belongs in
the file, under version control.

### Region assignment (`data_loader.py`)

Sites are grouped into peer regions by geography, not by hand: two sites join
the same region when their coordinates are within `region_radius_miles`
(`thresholds.toml`, default 10), regions are the connected components of that graph, and any
region with fewer than `min_region_site_count` sites is merged into its nearest
neighbour. The floor exists because scoring is peer-relative: a single-site
region (n = 2 colonies) makes z-scores degenerate (§2). With the current four
Watauga County sites this yields one region, `geo_region_01`.

---

## 2. Scoring architecture

### Peer-relative badness z-scores

Every colony gets a feature vector (`ColonyFeatures`) and is compared only
against the other colonies in its region over the same window. For each metric:

```
badness_z = (value − peer_mean) / peer_std      (sign flipped so worse > 0)
score     = min(100, weighted_mean(max(0, badness_z)) × 35)
```

Only *worse-than-peers* deviation accumulates (the `max(0, ·)` clamp); being
better than peers never subtracts. The scale factor 35 means "one standard
deviation worse than peers on average" ≈ 35/100. Because peer statistics
include the colony being scored, Samuelson's inequality bounds any single
z-score at √(n−1) for n colonies — the reason both the region-size floor and
the per-metric confidence labels exist.

### The metrics (`metrics.py`)

Five weight metrics, expert-elicited weights (robustness to ±20% perturbation
is validated in `spike_weight_sensitivity.py`; rankings shift ≤ 1–2 adjacent
positions, Kendall τ ≥ 0.93). Scoring is deliberately weight-only: internal
temperature and humidity feed data quality and event classification but carry
no scored metrics.

| Metric | Weight | Direction |
|---|---|---|
| Current colony weight | 0.45 | higher better |
| Weight % change (segmented, §3) | 0.26 | higher better |
| Weight % trend (segmented slope) | 0.14 | higher better |
| Favorable-weather weight trend | 0.09 | higher better |
| Poor-weather weight loss | 0.06 | lower better |

Weather-conditioned metrics only compare colonies that actually observed at
least one day of that weather type (`min_sample_attr` gating). When gating
shrinks a metric's peer pool to the degenerate case, the comparison is kept
but labelled `confidence: low` — a small pool is a property of the region, not
a colony fault, so it is surfaced rather than reweighted.

One metric family deserves a note:

- **Weather-conditioned trends** (`weather.py`): each hive-day is classified
  favorable / poor / neutral from Open-Meteo (rain codes, temperature band,
  cloud cover), and intraday weight changes are averaged separately per type.
  A colony that fails to gain on good foraging days is flagged even when its
  overall trend looks fine.

### Status assignment

`_status()` maps score + flags to `underperforming` (score ≥ 55 or ≥ 3
performance flags), `watch` (score ≥ 30, any performance flag, or a *persistent*
data-quality problem), or `normal`. Only *performance* flags drive the
underperforming cut — event flags ("Likely harvest…"), data-quality flags, and
low-confidence flags are informational, so a colony is never marked
underperforming just because its beekeeper harvested it or its sensor glitched.

Ahead of both cuts, `_apply_reporting_gaps()` forces `underperforming` on any
colony whose last reading is more than `status.max_reporting_gap_days` (default 1) older
than the newest reading in the cache. Two cases are handled: a colony with
readings in the window but a stale tail is annotated in place, while a colony
with no readings in the window at all is *materialised* by
`_not_reporting_score()` as a placeholder `ColonyScore` (score 0, empty
comparisons, `sample_count == 0`). The second case is the important one — the
feature builder only sees colonies with readings, so a silent hive would
otherwise be missing from the report rather than flagged in it, and absence
reads as health. `sample_count == 0` is the marker downstream consumers use to
exclude placeholders: sister comparison skips them, and `build_scores` computes
coverage metadata over reporting colonies only. `"Not reporting:"` flags are not
performance flags, so they never contribute to the ≥ 3-flag cut. Because
staleness is measured against the newest cached reading rather than the system
clock (a repo-wide invariant), an apiary-wide outage is invisible to this check.

Data-quality flags reach the watch cut only when the issues span more than
`status.quality_issue_day_share_threshold` (default 0.30) of the scoring window — more
than 2 distinct days out of 7, or more than 9 out of 30. The affected-day count
comes from `ColonyFeatures.data_quality_issue_days`, which `features.py` derives
via `quality.issue_dates()` over the colony's *untruncated* flag list. Isolated
bad readings occur in every deployment and say nothing about the colony; a fault
recurring across a large share of the window means the sensor needs attention
and the colony's metrics rest on thinner data than its peers'. The flags are
still printed in both cases — the gate governs status, not visibility.

### Sister comparison (`sister_comparison.py`)

A separate report compares the two colonies (L/R) at each site directly —
the tightest possible control pair (same location, same weather, same
management). This complements regional scoring and also powers event
corroboration (§3).

---

## 3. Event detection (`events.py`)

### Why it exists

The trend metrics assume weight moves smoothly. A harvest, swarm, or
supering shifts the baseline by several kilograms in an hour or two; a single
regression across that step reports a thriving colony as collapsing (harvest)
or a declining one as booming (supering). Event detection finds those step
discontinuities so they can be (a) excised from the trend and (b) surfaced as
explicit flags rather than silently averaged away.

### Detection rule: physical hard floors + persistence

Detection is deliberately simple — a fixed physical-magnitude rule rather than
a statistical outlier score (empirically grounded in `docs/METHODS.md`;
precision/recall/F1 = 1.000 against `ground_truth/events.json`):

1. **Candidate steps.** For each adjacent pair of readings ≤ 8 h apart
   (`MAX_EVENT_INTERVAL_HOURS`):
   - gain ≥ **3.0 kg** (`ADDITION_FLOOR_KG`) → candidate *addition* — a super
     or feeder is an object of known mass, while foraging returns rarely
     exceed 2–3 kg/h even on a strong flow day;
   - drop ≥ **3.0 %** of colony weight *and* ≥ **1.0 kg**
     (`HARVEST_FLOOR_PCT`, `HARVEST_FLOOR_KG`) → candidate *harvest/swarm*.
     The kg guard stops a small colony's noise from clearing the percentage;
     the % guard stops a big colony's noise from clearing the kg.
   - Pairs involving a non-positive weight are skipped: a 0.0 reading is a
     sensor dropout, not a level.
2. **Coalescing.** Candidates within 12 h of one another are merged into one
   net event (scales rarely land in one clean step — a harvest often reads
   drop-then-settle). The merged move must *still clear the floors*; a
   dip-then-full-recovery nets out to nothing and is dropped entirely. The
   merged event is anchored on the largest single step so the segment
   boundary lands on the real discontinuity.
3. **Persistence confirmation** (`_shift_persists`). The median weight over
   the 12 h after the step (`CONFIRMATION_WINDOW_HOURS`) must retain ≥ 50 %
   of the step (`CONFIRMATION_RETENTION`) in the same direction. A transient
   spike or lone dropout reverts and fails; a genuine baseline change holds.

### Classification

Upward steps are *additions* (management). Downward steps are split by
thermal signature: weight alone cannot distinguish a harvest from a swarm,
but a swarm removes a large mass of bees and disturbs the brood-nest climate,
so a drop paired with an internal temp swing ≥ 5 °F or humidity swing ≥ 10 pp
is labelled *swarm*, otherwise *harvest*. Labels are explicitly probabilistic
— reporting always says "Likely …" because the sensors cannot confirm cause.

### Sister corroboration — confidence, not detection

Apiary management is apiary-level: a beekeeper working one hive usually works
its neighbour in the same visit. Each colony is detected **independently**
with the full floors; afterwards, an event is tagged `corroborated` when its
sister colony also has a floor-clearing event within 8 h
(`CORROBORATION_WINDOW_HOURS`). This is direction-agnostic on purpose — a
joint harvest, joint supering, or an equalisation (harvest one side, feed the
other) are all one visit. Corroboration never lowers the detection bar, never
invents an event, and never removes one, so it contributes zero false
positives; it only labels how much apiary-level evidence supports an event.

### Segmentation: how events are neutralised in the trend

`segment_readings()` splits the window at each event timestamp; segments
shorter than 12 h are folded into their neighbour so trend fitting always has
data. `features._segmented_weight_trend()` then rebuilds the window-level
weight features from the segments:

- **Net change** = sum of each segment's own first-to-last change — the
  artificial step at each boundary is excluded, leaving the colony's organic
  gain/loss.
- **Trend** = span-weighted average of per-segment linear slopes, so a long
  stable stretch counts more than a short post-event tail and a harvest no
  longer drags the slope into a false "collapsing" reading.
- Weather-conditioned daily changes skip event days entirely, for the same
  reason.

With no events there is exactly one segment and everything reduces to the
naive first-vs-last / single-regression behaviour — a normal week is
unchanged.

---

## 4. Events vs. anomalous data: how the system tells them apart

Both a harvest and a scale glitch look like "weight changed a lot, fast."
The system separates them with three ideas: **detect events first**, **filter
with event-aware exemptions**, and **use asymmetric criteria** — what makes a
step an *event* (persistence, physical magnitude, corroboration) is exactly
what a sensor artifact lacks.

### 4.1 One detection pass, run before filtering

`prepare_features()` runs `detect_site_events()` on the **pre-filter**
windowed readings, and passes the result into *both* the quality filter and
the feature builder. This single-source-of-truth ordering matters twice over:

- If filtering ran first, the jump filter would excise the very readings that
  constitute a harvest step, and the event would never be seen.
- If each stage re-detected independently on its own slice of data, an event
  visible only with sister corroboration could be exempt in one stage and
  invisible in the other.

### 4.2 The quality filter's layers (`quality.py`)

Working per colony, in timestamp order:

1. **Impossible-range exclusion** (always applies, even inside event
   windows): weight outside 0.45–136 kg, internal temp outside 32–120 °F,
   humidity outside 0–100 %. These values cannot be real regardless of
   context. Out-of-range *external* sensor values are noted but do not
   exclude the reading — the internal measurements are still usable.
2. **Sudden-jump exclusion**: versus the last *kept* reading (≤ 6 h apart), a
   weight move > 3.63 kg *and* > 12 %, a temp move > 25 °F, or a humidity
   move > 45 pp excludes the reading as a sensor artifact. These thresholds
   are not guesses: `spike_quality_thresholds.py` grounds them against the
   full raw history (11,400 event-free adjacent pairs) — each sits at or
   above the 99.9th percentile of normal inter-reading movement, so the
   filter clips only genuine outliers.
3. **Event settle-window exemption**: readings within 3 h *after* a confirmed
   event timestamp (`EVENT_SETTLE_WINDOW_HOURS`) are exempt from the jump
   checks — a management visit disturbs weight, temperature, and humidity
   for a while, not just at the event instant. Range checks still apply.
4. **Consistent-run rescue** (`CONSISTENT_RUN_TO_ACCEPT = 3`): a safety net
   for real level shifts the detector missed (e.g. a sub-floor or
   slow-confirming shift). If three consecutive readings are each rejected
   versus the stale pre-jump anchor but are mutually consistent with one
   another (small steps between them), they are re-admitted as a sustained
   shift and a "Possible missed event" quality note is emitted. An isolated
   glitch does not resemble what follows it; a real new baseline does.

### 4.3 The decision surface, summarised

| Signal shape | Event detector | Quality filter | Outcome |
|---|---|---|---|
| Sharp step ≥ floors, new level persists ≥ 12 h | confirmed event | readings exempt (settle window) | flagged "Likely harvest/swarm/addition"; trend split at boundary |
| Same, sister colony steps within 8 h | confirmed + `corroborated` | exempt | as above, tagged high-confidence management |
| Spike that reverts within hours | fails persistence (and coalesces to net-zero) | jump filter excludes the spike readings | excluded as sensor artifact, quality note |
| Lone 0.0 / dropout reading | skipped (non-positive weight) | outside impossible range | excluded, quality note |
| Physically impossible value (e.g. 135 °F external) | n/a | range check | excluded or noted; never an event |
| Sustained shift *below* the event floors | not an event | jump filter fires, then consistent-run rescue re-admits | data kept; "Possible missed event" note |
| Ordinary foraging gain / nightly loss / drift | below floors | below jump thresholds | normal data, one segment |

The asymmetry is the point: an **event** must be *large* (physical floors),
*fast* (≤ 8 h), and *persistent* (12 h median retention) — and is often
*corroborated* by the sister colony. An **artifact** is transient, isolated,
reverts, or is physically impossible. The narrow band between them —
sustained shifts too small or too slow to confirm — is deliberately kept in
the data and surfaced as a note for human review rather than silently decided
either way.

Everything that is excluded or exempted leaves a trace: excluded-reading
counts and per-colony quality notes flow into the feature vector, appear as
flags on the colony's score, and are counted in the run metadata — so a
reviewer can always reconstruct what the filter did and why.

---

## 5. Reporting

`reporting.py` renders the regional ranking (score, status, top-3 worst
metric comparisons, flags, and the event log per colony); the JSON outputs
(`output/scoring.json`, `output/sister_comparisons.json`) carry the full
feature vectors, per-metric comparisons with peer counts and confidence, and
structured event records (`kind`, timestamps, before/after kg, `corroborated`)
for downstream analysis.
