"""Diagnostic spike: sensitivity of colony rankings to the metric weights.

The 9 metric weights in beemon_scoring/metrics.py (0.30 current weight, 0.17
percent change, ...) are expert-elicited, not learned. A reviewer will ask how
much the conclusions depend on those exact numbers. This script perturbs the
weights and measures how stable the within-region colony *ranking* and *status*
assignments are -- the defensible robustness claim for the paper (weights are
not optimal, but the ordering they produce is not fragile to them).

Two perturbation modes:
  * one-at-a-time: multiply each metric's weight by {0.5, 0.75, 1.25, 1.5} in
    turn (36 deterministic variants), holding the others fixed.
  * dirichlet: sample whole weight vectors around the baseline with stdlib
    gammavariate (seeded), at two concentrations (~+-10% and ~+-20% jitter).

Modeling notes (why this is cheap and correct):
  * _score_region_features divides the weighted badness by total_weight, so the
    weights are purely *relative* -- no renormalization is needed and scaling
    all weights equally is a no-op.
  * badness_z is computed independently of the weights, so per-metric flags
    ("... standard deviations worse than peers") never change under perturbation;
    only the aggregate 0-100 score, the 30/55 status cuts, and the ranking move.
  * prepare_features runs the whole pipeline once; each variant only re-runs the
    cheap _score_features arithmetic.

Usage:
    python3 spike_weight_sensitivity.py
    python3 spike_weight_sensitivity.py --window-days 60 --draws 500 --seed 20260727
    python3 spike_weight_sensitivity.py --json output/weight_sensitivity.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import random
from collections import defaultdict
from pathlib import Path

from beemon_scoring.metrics import METRICS
from beemon_scoring.scoring import _score_features, prepare_features

PROJECT_ROOT = Path(__file__).resolve().parent

ONE_AT_A_TIME_FACTORS = [0.5, 0.75, 1.25, 1.5]
DIRICHLET_CONCENTRATIONS = [100.0, 20.0]  # ~+-10% and ~+-20% relative jitter


def _ranking_by_region(scores) -> dict[str, list[str]]:
    """Within-region colony order by (-score, colony_id) -- the same tie-break
    build_scores uses."""
    by_region: dict[str, list] = defaultdict(list)
    for score in scores:
        by_region[score.region_id].append(score)
    return {
        region_id: [s.colony_id for s in sorted(region_scores, key=lambda s: (-s.score, s.colony_id))]
        for region_id, region_scores in by_region.items()
    }


def _status_by_colony(scores) -> dict[str, str]:
    return {s.colony_id: s.status for s in scores}


def _score_by_colony(scores) -> dict[str, float]:
    return {s.colony_id: s.score for s in scores}


def _kendall_tau(order_a: list[str], order_b: list[str]) -> float:
    """Kendall rank correlation between two orderings of the same items."""
    rank_b = {item: i for i, item in enumerate(order_b)}
    items = [item for item in order_a if item in rank_b]
    n = len(items)
    if n < 2:
        return 1.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            # items are already in order_a order, so a ranks i before j for i<j;
            # the pair is concordant when b agrees (ranks i before j too).
            b_order = rank_b[items[i]] - rank_b[items[j]]
            if b_order < 0:
                concordant += 1
            elif b_order > 0:
                discordant += 1
    total = n * (n - 1) / 2
    return (concordant - discordant) / total if total else 1.0


def _perturbed_metrics(weights: list[float]) -> list:
    return [dataclasses.replace(metric, weight=w) for metric, w in zip(METRICS, weights)]


def _compare(baseline_scores, variant_scores) -> dict:
    base_rank = _ranking_by_region(baseline_scores)
    var_rank = _ranking_by_region(variant_scores)
    base_status = _status_by_colony(baseline_scores)
    var_status = _status_by_colony(variant_scores)

    rank_unchanged = all(base_rank[r] == var_rank.get(r) for r in base_rank)
    status_changes = sum(1 for c in base_status if var_status.get(c) != base_status[c])
    max_rank_shift = 0
    for region_id, order in base_rank.items():
        var_order = var_rank.get(region_id, [])
        pos = {c: i for i, c in enumerate(var_order)}
        for i, colony in enumerate(order):
            if colony in pos:
                max_rank_shift = max(max_rank_shift, abs(pos[colony] - i))
    mean_tau = (
        sum(_kendall_tau(base_rank[r], var_rank.get(r, [])) for r in base_rank) / len(base_rank)
        if base_rank
        else 1.0
    )
    return {
        "rank_unchanged": rank_unchanged,
        "status_changes": status_changes,
        "max_rank_shift": max_rank_shift,
        "mean_kendall_tau": mean_tau,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=None)
    parser.add_argument("--draws", type=int, default=500, help="Dirichlet draws per concentration.")
    parser.add_argument("--seed", type=int, default=20260727)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    features, settings, prep = prepare_features(PROJECT_ROOT, window_days=args.window_days)
    baseline_scores = _score_features(features, settings, METRICS)
    baseline_weights = [m.weight for m in METRICS]

    # Control: an unperturbed rescore must reproduce the baseline exactly.
    control = _score_features(features, settings, _perturbed_metrics(baseline_weights))
    assert _score_by_colony(control) == _score_by_colony(baseline_scores), "control rescore diverged"

    print(f"\nWeight sensitivity  (window={prep['window_days']}d, colonies={len(baseline_scores)})")
    print("Baseline ranking by region:")
    for region_id, order in _ranking_by_region(baseline_scores).items():
        print(f"  {region_id}: {' > '.join(order)}")

    # --- One-at-a-time ---
    print("\n=== One-at-a-time weight perturbation ===")
    oat_results = []
    for i, metric in enumerate(METRICS):
        for factor in ONE_AT_A_TIME_FACTORS:
            weights = list(baseline_weights)
            weights[i] = baseline_weights[i] * factor
            variant = _score_features(features, settings, _perturbed_metrics(weights))
            result = _compare(baseline_scores, variant)
            oat_results.append(result)
            if not result["rank_unchanged"] or result["status_changes"]:
                print(
                    f"  {metric.name} x{factor}: rank_unchanged={result['rank_unchanged']} "
                    f"status_changes={result['status_changes']} max_shift={result['max_rank_shift']} "
                    f"tau={result['mean_kendall_tau']:.3f}"
                )
    oat_stable = sum(1 for r in oat_results if r["rank_unchanged"] and not r["status_changes"])
    print(f"  {oat_stable}/{len(oat_results)} variants left ranking AND status fully unchanged.")

    # --- Dirichlet ---
    print("\n=== Dirichlet weight sampling ===")
    dirichlet_summary = {}
    for concentration in DIRICHLET_CONCENTRATIONS:
        rng = random.Random(args.seed + int(concentration))
        rank_stable = status_stable = 0
        taus = []
        max_shift = 0
        for _ in range(args.draws):
            gammas = [rng.gammavariate(concentration * w, 1.0) for w in baseline_weights]
            total = sum(gammas)
            weights = [g / total for g in gammas]
            variant = _score_features(features, settings, _perturbed_metrics(weights))
            result = _compare(baseline_scores, variant)
            rank_stable += 1 if result["rank_unchanged"] else 0
            status_stable += 1 if result["status_changes"] == 0 else 0
            taus.append(result["mean_kendall_tau"])
            max_shift = max(max_shift, result["max_rank_shift"])
        approx_jitter = "~+-10%" if concentration >= 100 else "~+-20%"
        summary = {
            "draws": args.draws,
            "rank_unchanged_pct": round(100 * rank_stable / args.draws, 1),
            "status_unchanged_pct": round(100 * status_stable / args.draws, 1),
            "mean_kendall_tau": round(sum(taus) / len(taus), 4),
            "min_kendall_tau": round(min(taus), 4),
            "max_rank_shift": max_shift,
        }
        dirichlet_summary[f"concentration_{int(concentration)}"] = summary
        print(
            f"  c={int(concentration)} ({approx_jitter}): ranking unchanged in "
            f"{summary['rank_unchanged_pct']}% of {args.draws} draws, status unchanged in "
            f"{summary['status_unchanged_pct']}%, mean tau={summary['mean_kendall_tau']}, "
            f"min tau={summary['min_kendall_tau']}, max rank shift={summary['max_rank_shift']}"
        )

    if args.json:
        payload = {
            "window_days": prep["window_days"],
            "baseline_ranking": _ranking_by_region(baseline_scores),
            "one_at_a_time_variants": len(oat_results),
            "one_at_a_time_fully_stable": oat_stable,
            "dirichlet": dirichlet_summary,
        }
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
