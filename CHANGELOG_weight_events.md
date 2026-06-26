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
