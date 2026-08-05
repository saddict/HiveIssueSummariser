# Changelog: weight-event–aware scoring

## Problem

Colony weight features (`weight_delta_kg`, `weight_pct_change`,
`weight_slope_kg_per_day`, `weight_slope_pct_per_day`, and the
poor-/favorable-weather metrics) were computed across the **entire** rolling
window as a single first-vs-last delta and one straight-line regression. Any
abrupt management event — a honey harvest, a swarm, or adding a super/feeder —
injects a step discontinuity into that line and produces wrong results:

- A harvest (e.g. 40 kg → 30 kg) made a healthy, gaining colony look like it was
  collapsing.
- Supering made a flat or declining colony look like it was booming.
- The event was **never surfaced** to the beekeeper.

Worse, `quality.py` treated sharp drops as "sudden jump" sensor faults and
**excluded** them, so a real harvest was both mislabeled as noise and silently
removed — quietly corrupting the surviving trend.

On the bundled real data, `PRT_1:R` reported `-5.4%` and its -15 kg harvest was
discarded as a data-quality jump. The colony had actually *gained* weight across
its stable phases and then been harvested.

## Fix

New module `beemon_scoring/events.py`:

- `detect_weight_events` finds sharp, sustained level shifts (clearing both an
  absolute and a relative floor, within a short interval, and persisting through
  a confirmation window). Clustered steps (drop-then-settle, dip-then-recover)
  are coalesced into a single net event; transients that recover net out to
  nothing. Each event is classified `harvest` / `swarm` / `addition`.
- `segment_readings` splits the window at each event into stable segments.

`beemon_scoring/features.py`:

- Weight trend is now a **span-weighted average of per-segment slopes**; net
  change is the **sum of within-segment changes** — both exclude the step
  itself. This is the "sample each part, score it, then average" approach.
- `daily_weight_pct_changes` skips days on which an event occurred.
- `ColonyFeatures` gains `weight_event_count`, `weight_event_descriptions`, and
  `segment_count`.

`beemon_scoring/quality.py`:

- Confirmed event steps now **pass through** the filter instead of being
  excluded as sudden jumps (recovered 12 legitimate readings on the sample
  data). Impossible readings (0.0 kg, out-of-range) are still excluded.

`beemon_scoring/scoring.py` and reporting:

- Events are surfaced as flags (`Likely harvest: ...`). They are treated as
  informational, not performance penalties, so a harvest no longer pushes a
  colony toward "underperforming". Window-level event counts added to metadata.

A normal week contains no events, reduces to a single segment, and reproduces
the original behaviour exactly.

## Result on sample data (`PRT_1:R`)

| | weight reported | harvest surfaced | status |
|---|---|---|---|
| Before any fix | `-5.4%` (false loss) | no — excluded as noise | watch |
| After fix | `+9.9%` (organic gain) | yes — harvest + supering flagged | watch (score 0.0) |

## Tests

`tests/test_weight_events.py` (12 cases): clean harvest detection and
segmentation, organic-trend recovery, supering vs swarm classification, normal
week unchanged, transient dip ignored, drop-then-settle coalescing, zero-weight
dropout handling, quality-filter pass-through, event-day skipping, and short
post-event segment merging. All 23 tests in the suite pass.

## Update (2026-07-27): event detection vs. data-quality clash

The MAD detector and the quality filter each ran their own event detection over
different slices of the data, so a corroborated event visible only to the
site-level detector could still be excluded by the quality filter as a "sudden
jump" — the two disagreed about what counted as an event. This pass makes the
detected event set a single source of truth and extends corroboration and the
quality filter to match.

`beemon_scoring/events.py`:

- **Symmetric sister corroboration.** `corroborate_sister_events` promotes
  same-sign sub-threshold moves in *both* directions (gain↔gain as well as
  drop↔drop), and rescues gains that cleared the MAD z-bar but were blocked only
  by the absolute addition floor.
- **Paired corroboration** (`_promote_paired_events`). When *neither* sister has
  an individually confirmed event but both show a same-direction sub-threshold
  step within one reading interval of each other, the timing itself is the
  evidence and both are promoted. Gated per side by `PAIRED_MIN_GAIN_KG=1.5` /
  `PAIRED_MIN_GAIN_PCT=3.0` for gains (drops need no extra floor), since sisters
  share weather and forage and a shared foraging burst would otherwise pass.
- **Aggregate multi-step pass** (`_aggregate_candidates` /
  `_aggregate_window_shares`). A gradual move split into individually sub-floor
  hourly steps is caught by a second rolling `AGGREGATE_SPAN_HOURS=3.0` scan on
  its net change, with `AGGREGATE_MAX_DOMINANT_STEP_SHARE=0.6` rejecting a window
  whose single largest step dominates the net (an ordinary trend containing one
  already-blocked spike).
- `detect_site_events` is the single entry point: group by site → detect per
  side → corroborate → coalesce.

`beemon_scoring/quality.py`:

- Consumes the shared `events_by_colony` set (including corroborated events its
  own per-colony detection could not see) instead of re-detecting.
- **Settle window** (`EVENT_SETTLE_WINDOW_HOURS=3.0`): post-event readings, not
  just the exact event timestamp, are exempt from jump checks — a visit disturbs
  the sensor for hours.
- **Anti-cascade** (`CONSISTENT_RUN_TO_ACCEPT=3`): a run of mutually consistent
  readings excluded against a stale anchor (a real level shift the detector
  missed) is re-admitted, while a lone fault stays excluded.

`beemon_scoring/scoring.py`: `build_scores` runs `detect_site_events` once, up
front, on the pre-filter readings and threads that one result into both
`filter_quality_issues` and `build_features`.

### Threshold validation (Step 7)

The provisional promotion floors were locked against a 60-day sweep over all
four sites (`spike_paired_corroboration.py`, 2026-07-27):

- `PAIRED_MIN_GAIN_KG=1.5` / `PAIRED_MIN_GAIN_PCT=3.0` — the false-pair
  population (shared-foraging dual co-gains; post-harvest settle-back rebounds)
  tops out at 1.21 kg/side and 1.20 kg/side respectively, both rejected at
  1.5 kg; the smallest genuine paired step is 1.6 kg.
- `AGGREGATE_MAX_DOMINANT_STEP_SHARE=0.6` — genuine multi-step moves land at a
  dominant-step share ≤ 0.53; single-step-dominated windows (including the
  WTG_HSCHL:R foraging burst at 0.78) land ≥ 0.60.

### Tests

`tests/test_event_quality_clash.py` (9 cases) pins the acceptance criteria:
paired sub-threshold additions, the foraging-burst false-positive guard,
quality-filter exemption of corroborated events, end-to-end survival into
`weight_event_count`, stale-anchor no-cascade, the settle window, aggregate
split-addition plus a smooth-nectar-flow guard, and symmetric gain
corroboration. All 47 tests in the suite pass.

## Simplification: hard floors + confidence corroboration (2026-07-31)

The MAD + aggregate + sister/paired-**promotion** design above was replaced with
a simple hard-floor detector after review against `ground_truth/events.json`
showed the machinery produced 5 unvalidated sub-1 kg events (none in the ground
truth) while all 6 real events were caught by physical floors alone.

- **Detection:** an adjacent step is an event when it clears a fixed floor
  (`ADDITION_FLOOR_KG=3.0`, or a drop ≥ `HARVEST_FLOOR_PCT=3.0` **and**
  ≥ `HARVEST_FLOOR_KG=1.0`), coalesced and persistence-confirmed. No MAD, no
  z-score, no aggregate pass.
- **Corroboration → confidence:** each colony is detected independently; an event
  is then tagged `corroborated` (new `WeightEvent.corroborated` field) when the
  sister has a floor-clearing event within `CORROBORATION_WINDOW_HOURS`,
  **direction-agnostic** (joint work or equalisation). It never lowers the bar,
  so it adds zero false positives.
- **Result:** 27 → 20 events; precision/recall/F1 = 1.000 preserved; `events.py`
  918 → ~380 lines. Suite 56/56 (MAD tests removed; `test_corroboration.py`
  added; the clash tests refocused on the quality/feature wiring). Full detail in
  `PROJECT_HANDOFF.md` §23 and `docs/METHODS.md`.
