# Methods & Provenance

A methods-section-ready map of every metric, threshold, and statistical method in
the BeeMon scoring system to its scientific or mathematical basis. This is the
grounding a paper/thesis reviewer will check first. Each row is either **cited**
(published basis) or **empirical** (derived from this project's own data, with the
reproducing script named).

> **Citation-verification note.** Author/venue/year below are given to the best of
> current knowledge but have **not** been checked against the primary sources in
> this environment. Verify every citation (exact year, volume, pages, DOI) before
> submission. Items marked *(verify)* are ones to confirm most carefully.

---

## 1. Biological grounding of the metrics

| Metric (code name) | Basis | Reference |
|---|---|---|
| Hive weight as a colony-strength / foraging signal — `latest_weight_kg`, `weight_pct_change`, `weight_slope_pct_per_day`, and the weather-adjusted trends | Continuous hive-weight monitoring is an established proxy for colony population, foraging activity, and net resource gain; daily and seasonal weight dynamics track flow and colony state. | Meikle, W.G. & Holst, N. (2015), *Application of continuous monitoring of honey bee colonies*, **Apidologie** 46(1):10–22. Supporting: Meikle et al. on hive-weight time-series analysis *(verify specific papers)*. |
| Abrupt weight step = harvest / swarm / supering / feeding | Management actions and swarming produce sharp, sustained level shifts in hive weight distinct from the smooth daily cycle; detecting them as discontinuities (not trend) is standard in weight-monitoring analyses. | Meikle & Holst (2015) as above; weight-based swarm/harvest detection literature *(verify + add specific swarm-detection cite)*. |
| Internal temperature & humidity used only for data quality and event classification (no scored metrics) | Scoring is deliberately weight-only. Temperature/humidity thresholds from the literature (thermal-efficiency floors, brood-band variance, RH bands) did not transfer to this deployment's sensor placement when checked empirically, so those candidate metrics were removed rather than shipped unvalidated. Internal climate still gates data quality (`quality.py` bounds/jump checks) and separates swarm from harvest in event classification (`events.py:_classify`). | Design decision (2026-08-17), recorded in `PROJECT_HANDOFF.md`. Swarm-signature basis: brood-nest climate disturbance accompanies bee-mass loss *(verify cite if kept in the paper)*. |

**Action for the author:** confirm whether this is the **Appalachian State University
BeeMon** deployment (all sites in Watauga County, NC). If so, prior BeeMon group
publications must be cited and positioned against in Related Work — a committee
will expect it, and it may pre-empt a novelty challenge.

---

## 2. Statistical & mathematical method provenance

| Method | Basis | Reference / evidence |
|---|---|---|
| Hard-floor event detection (`events.py`, `ADDITION_FLOOR_KG`, `HARVEST_FLOOR_PCT`, `HARVEST_FLOOR_KG`) | A weight step is an event when it clears a fixed physical magnitude within `MAX_EVENT_INTERVAL_HOURS` and then persists (`_shift_persists`): additions ≥ 3 kg (a super/feeder is an object of known mass), harvests ≥ 3% of colony weight and ≥ 1 kg. Physical thresholds rather than statistical outlier scores — simpler and directly defensible; the floors sit above the observed foraging-burst maximum (≈2.7 kg) so ordinary movement is excluded by magnitude. | Level-shift interpretation: Meikle & Holst (2015). Floor grounding: the WTG_HSCHL foraging non-event peaks at +2.7 kg (below the 3 kg floor); the smallest confirmed harvest is −4.0% (6LR:R, 2026-07-07). Validation: `validate_events.py` against `ground_truth/events.json` (precision/recall/F1 = 1.000). |
| Sister corroboration *as a confidence signal* (`_tag_corroboration`, `CORROBORATION_WINDOW_HOURS`) | Apiary management actions are apiary-level: a beekeeper working one hive usually works its neighbour in the same visit. Each colony is detected **independently** with the hard floors; an event is then tagged `corroborated` when the sister also has a floor-clearing event within the window — **direction-agnostic** (joint management or equalisation: harvest one side, feed/super the other). Corroboration never lowers the detection bar and never invents an event, so it adds **zero** false positives; it labels apiary-level support and is itself validatable (corroborated vs isolated precision). | Biological prior: Meikle & Holst (2015) (apiary-scale management). Evidence in this dataset: all 6 ground-truth events are corroborated L/R pairs (incl. the PRT_1 2026-06-23 equalisation: L +11 kg / R −15 kg, same day); isolated anomalies (e.g. DR_WLKS:L −25 kg) are correctly untagged. |
| Peer-relative z-score ranking (`scoring.py`) | Relative anomaly scoring within a geographic peer group. Self-inclusion bounds any single z at `√(n−1)` (Samuelson's inequality) — stated as an explicit limitation motivating `MIN_REGION_SITE_COUNT` and the per-metric confidence labels. | Samuelson, P.A. (1968), *How Deviant Can You Be?*, **JASA** 63:1522–1525. Degenerate-case handling: `PROJECT_HANDOFF.md` §16, §19; `tests/test_metric_confidence.py`. |
| Weighted-sum aggregation of the 5 weight metrics (`metrics.py`) | Multi-criteria decision analysis with expert-elicited weights. Defensibility comes not from optimality but from a **sensitivity analysis** showing the output is robust to the weights. | MCDA / global sensitivity analysis: Saltelli, A. et al. (2008), *Global Sensitivity Analysis: The Primer*, Wiley *(verify)*. Evidence: `spike_weight_sensitivity.py` — status 100% invariant to ±20% weight perturbation; ranking Kendall τ ≥ 0.93 over a 60-day window. |
| Region assignment (`data_loader.py`) | Connected components of a distance graph (`REGION_RADIUS_MILES`) with a minimum-peer-count merge to avoid degenerate small pools. | Haversine distance; project design (`PROJECT_HANDOFF.md` §13, §16). |
| Data-quality jump thresholds (`quality.py`) | Each threshold justified against an observed percentile of real inter-reading movement (all ≥ p99.9), with events exempt from the filter; physical bounds justified as impossible, not statistical. | Empirical: `spike_quality_thresholds.py` (`PROJECT_HANDOFF.md` §22). |
| Data-quality **duration** gate on status (`scoring._quality_issue_days_material`, `QUALITY_ISSUE_DAY_SHARE_THRESHOLD`) | Quality problems only move a colony to `watch` when they span more than 30% of the scoring window (> 2 days of 7). Isolated bad readings are ubiquitous and carry no colony-level information; a fault recurring over a large share of the window both signals failing hardware and thins the evidence base the colony's metrics are computed from. Reporting is unaffected — the gate applies to status only. | Design decision (2026-08-19), recorded in `PROJECT_HANDOFF.md` §25. Effect on this dataset: over a 90-day window it demotes two colonies whose issues covered ≤ 10 of 90 days from `watch` to `normal`; no 7-day-window colony is affected. |

---

## 3. Validation strategy

- **Event detection** is validated with precision / recall / F1 and a confusion
  matrix against beekeeper ground truth (`validate_events.py`,
  `ground_truth/events.json`). On the seeded confirmed subset the detector scores
  1.000 across the board; the ground-truth file must be **extended from the full
  inspection log** to make this the headline result over the whole season.
- **Scoring robustness** is defended by the weight sensitivity analysis
  (`spike_weight_sensitivity.py`), reported as status-invariance and rank
  correlation under weight perturbation.
- **Threshold choices** (event-promotion and data-quality) are each traced to an
  observed data distribution via the named spike scripts, not asserted.

---

## 4. Honest limitations (for the paper)

- **External validity.** 8 colonies at one merged region near Boone, NC. The
  peer-relative design is honest about scope, but generalisation claims must be
  cautious; more sites/regions is the real fix.
- **Weather adjustment is coarse** — whole-day favorable/poor/neutral from ~3
  samples/day, no nectar-flow or forage model.
- **Event labels are probabilistic.** harvest vs swarm vs addition is inferred
  from weight (and, for swarm, a climate disturbance); sensors cannot confirm the
  cause.
- **Detection floors are fixed, not adaptive.** The 3 kg / 3%+1 kg floors are
  physical thresholds tuned so they clear the observed foraging maximum; a
  colony on an exceptionally strong flow could in principle gain > 3 kg in an
  hour and be flagged. Detection is also single-step (adjacent readings) plus
  settle-back coalescing — a genuine event spread gradually into several
  sub-floor steps would be missed. Neither case occurs in the current dataset,
  and the trade for simplicity/explainability is deliberate.
- **Scoring is weight-only.** Internal temperature and humidity carry no scored
  metrics (literature thresholds did not transfer to this deployment), so a
  purely thermoregulatory decline will not surface unless it also moves weight.
- **Metric weights are expert-elicited, not learned** — mitigated, not eliminated,
  by the sensitivity analysis.

Most of these already appear in `README.md` ("Current Limitations") and can be
lifted and sharpened.
