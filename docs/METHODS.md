# Methods & Provenance

A methods-section-ready map of every metric, threshold, and statistical method in
the BeeMon scoring system to its scientific or mathematical basis. This is the
grounding a paper/thesis reviewer will check first. Each row is either **cited**
(published basis) or **empirical** (derived from this project's own data, with the
reproducing script named).

> **Citation-verification note.** Author/venue/year below are given to the best of
> current knowledge but have **not** been checked against the primary sources in
> this environment. Verify every citation (exact year, volume, pages, DOI) before
> submission. Items marked *(verify)* are ones to confirm most carefully. The
> thermal-efficiency reference is cited as it already appears in the code
> (`thermal.py`); confirm the exact Kovac & Stabentheiner reference and year.

---

## 1. Biological grounding of the metrics

| Metric (code name) | Basis | Reference |
|---|---|---|
| Hive weight as a colony-strength / foraging signal — `latest_weight_kg`, `weight_pct_change`, `weight_slope_pct_per_day`, and the weather-adjusted trends | Continuous hive-weight monitoring is an established proxy for colony population, foraging activity, and net resource gain; daily and seasonal weight dynamics track flow and colony state. | Meikle, W.G. & Holst, N. (2015), *Application of continuous monitoring of honey bee colonies*, **Apidologie** 46(1):10–22. Supporting: Meikle et al. on hive-weight time-series analysis *(verify specific papers)*. |
| Abrupt weight step = harvest / swarm / supering / feeding | Management actions and swarming produce sharp, sustained level shifts in hive weight distinct from the smooth daily cycle; detecting them as discontinuities (not trend) is standard in weight-monitoring analyses. | Meikle & Holst (2015) as above; weight-based swarm/harvest detection literature *(verify + add specific swarm-detection cite)*. |
| Thermal efficiency — `thermal_efficiency_pi`, `thermal_efficiency_m`, ΔT (already grounded) | The linear internal-vs-external temperature model `T_H = m·T_E + ΔT` separates passive weather-tracking (`m`) from active metabolic lift (`ΔT`); `Pi = ΔT/34.5 °C` normalises the lift. | Kovac & Stabentheiner (thermal model, as cited in `thermal.py`) *(verify year/venue)*. Brood-temperature basis: Kleinhenz, Bujok, Fuchs & Tautz (2003), *Hot bees in empty broodnest cells*, **J. Exp. Biol.** 206:4217–4231. |
| Temperature instability — `internal_temp_std_f` | Healthy colonies hold brood-nest temperature within a narrow band (~34–35 °C); elevated variance indicates weaker thermoregulation. Peer-relative, so cold-week drift affects all colonies equally and cancels. | Stabentheiner, Kovac & Brodschneider (2010), *Honeybee colony thermoregulation…*, **PLoS ONE** 5(1):e8967. Jones, Myerscough, Graham & Oldroyd (2004), *Honey bee nest thermoregulation: diversity promotes stability*, **Science** 305:402–404. |
| Humidity exposure & instability — `high_humidity_reading_pct` (>70%), `low_humidity_reading_pct` (<40%), `internal_humidity_std_pct` | Colonies actively influence nest humidity; brood viability has a preferred RH band, and sustained extremes / instability are stressors. **The 70% / 40% cut-points need a cited RH band or should be replaced with one.** | Human, Nicolson & Dietemann (2006), *Do honey bees regulate humidity in their nest?*, **Naturwissenschaften** 93:397–401. *(verify the exact viable-RH band to justify 70/40)*. |

**Action for the author:** confirm whether this is the **Appalachian State University
BeeMon** deployment (all sites in Watauga County, NC). If so, prior BeeMon group
publications must be cited and positioned against in Related Work — a committee
will expect it, and it may pre-empt a novelty challenge.

---

## 2. Statistical & mathematical method provenance

| Method | Basis | Reference / evidence |
|---|---|---|
| MAD robust-z event detection (`events.py`, `MAD_SENSITIVITY_K`, `MAD_CORROBORATE_K`) | The modified z-score `0.6745·(x − median)/MAD` is the standard robust outlier score; MAD is preferred over SD because it is not inflated by the very outliers being detected. Express the chosen `k` in this framework and state the implied false-positive expectation. | Iglewicz, B. & Hoaglin, D.C. (1993), *How to Detect and Handle Outliers*, ASQC Quality Press. Leys, Ley, Klein, Bernard & Licata (2013), **J. Exp. Soc. Psychol.** 49:764–766. Threshold calibration: `spike_mad_events.py`. |
| Sister & paired corroboration (`corroborate_sister_events`, `_promote_paired_events`) — **the novel contribution** | Apiary management actions are paired-colony/apiary-level: a beekeeper working one hive usually works its neighbour. Temporal + directional co-occurrence at co-located sisters is therefore evidence for a real event; the shared weather/forage confound is controlled by the per-side gain floors. Frame precisely — this is the paper's core idea. | Biological prior: Meikle & Holst (2015) (apiary-scale management). Threshold grounding + false-pair analysis: `spike_paired_corroboration.py` (60-day sweep). Validation: `validate_events.py` against `ground_truth/events.json`. |
| Aggregate multi-step detection (`_aggregate_candidates`, `AGGREGATE_MAX_DOMINANT_STEP_SHARE`) | A gradual move split into sub-floor hourly steps is caught on its net change over a short span, with a dominant-step-share guard so an ordinary trend containing one blocked spike is not diluted into a false positive. | Empirical: `spike_paired_corroboration.py` aggregate-share scan (genuine multi-step ≤0.53 vs single-step-dominated ≥0.60). |
| Peer-relative z-score ranking (`scoring.py`) | Relative anomaly scoring within a geographic peer group. Self-inclusion bounds any single z at `√(n−1)` (Samuelson's inequality) — stated as an explicit limitation motivating `MIN_REGION_SITE_COUNT` and the per-metric confidence labels. | Samuelson, P.A. (1968), *How Deviant Can You Be?*, **JASA** 63:1522–1525. Degenerate-case handling: `PROJECT_HANDOFF.md` §16, §19; `tests/test_metric_confidence.py`. |
| Weighted-sum aggregation of the 9 metrics (`metrics.py`) | Multi-criteria decision analysis with expert-elicited weights. Defensibility comes not from optimality but from a **sensitivity analysis** showing the output is robust to the weights. | MCDA / global sensitivity analysis: Saltelli, A. et al. (2008), *Global Sensitivity Analysis: The Primer*, Wiley *(verify)*. Evidence: `spike_weight_sensitivity.py` — status 100% invariant to ±20% weight perturbation; ranking Kendall τ ≥ 0.93 over a 60-day window. |
| Region assignment (`data_loader.py`) | Connected components of a distance graph (`REGION_RADIUS_MILES`) with a minimum-peer-count merge to avoid degenerate small pools. | Haversine distance; project design (`PROJECT_HANDOFF.md` §13, §16). |
| Data-quality jump thresholds (`quality.py`) | Each threshold justified against an observed percentile of real inter-reading movement (all ≥ p99.9), with events exempt from the filter; physical bounds justified as impossible, not statistical. | Empirical: `spike_quality_thresholds.py` (`PROJECT_HANDOFF.md` §22). |

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
- **Thermal efficiency measures thermoregulatory behaviour, not brood presence
  or health** directly, and cannot know sensor placement or queen status.
- **Metric weights are expert-elicited, not learned** — mitigated, not eliminated,
  by the sensitivity analysis.

Most of these already appear in `README.md` ("Current Limitations") and can be
lifted and sharpened.
