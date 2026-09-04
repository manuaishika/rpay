"""sweep.py -- the thesis stress-test.

The whole argument rests on two assumed numbers: the voice LIFT (how much a
call actually helps -- world.py) and the voice COST (~Rs.12). This sweeps
both, runs every policy in every cell, and reports where the sequential
agent stops beating the best fixed playbook.

    python -m recovery.sweep                 # table + audit/sweep.json
    python -m recovery.sweep --json          # just the JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import core
from .analysis import run_all
from .guardrails import DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET

STRESS_GRID = [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6]
COST_GRID = [6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 24.0]
BASELINE = "standard_playbook"          # best fixed playbook to beat


def _cell(seed, stress, cost, voice_budget, human_cap) -> dict:
    with core.voice_cost(cost):
        res = run_all(seed, stress, voice_budget, human_cap)
    by = {p["policy"]: p for p in res["policies"]}
    ladder, base = by["ladder"], by[BASELINE]
    return {
        "stress": stress,
        "voice_cost": cost,
        "ladder_net": ladder["net"],
        "baseline_net": base["net"],
        "ladder_minus_baseline": round(ladder["net"] - base["net"], 2),
        "ladder_calls": ladder["calls"],
        "ladder_cost_per_100": ladder["cost_per_100"],
        "ladder_wins": ladder["net"] >= base["net"],
        "violations": res["total_violations"],
        "nets": {p["policy"]: p["net"] for p in res["policies"]},
    }


def run_sweep(seed=20260903, voice_budget=DEFAULT_VOICE_BUDGET, human_cap=DEFAULT_HUMAN_CAP,
              stress_grid=None, cost_grid=None) -> dict:
    stress_grid = stress_grid or STRESS_GRID
    cost_grid = cost_grid or COST_GRID
    cells = [_cell(seed, s, c, voice_budget, human_cap)
             for s in stress_grid for c in cost_grid]

    # crossover at the default cost: lowest stress where ladder still wins
    at_cost12 = sorted((c for c in cells if c["voice_cost"] == 12.0),
                       key=lambda c: c["stress"])
    stress_floor = next((c["stress"] for c in at_cost12 if c["ladder_wins"]), None)
    # crossover at the default stress: highest cost where ladder still wins
    at_stress1 = sorted((c for c in cells if c["stress"] == 1.0),
                        key=lambda c: c["voice_cost"])
    cost_ceiling = next((c["voice_cost"] for c in reversed(at_stress1)
                         if c["ladder_wins"]), None)

    return {
        "seed": seed,
        "voice_budget": voice_budget,
        "human_cap": human_cap,
        "baseline": BASELINE,
        "stress_grid": stress_grid,
        "cost_grid": cost_grid,
        "cells": cells,
        "crossover": {
            "min_stress_ladder_wins_at_cost_12": stress_floor,
            "max_cost_ladder_wins_at_stress_1": cost_ceiling,
            "ladder_wins_everywhere": all(c["ladder_wins"] for c in cells),
        },
    }


def _print(sweep: dict):
    print(f"sweep: seed={sweep['seed']}  baseline={sweep['baseline']}  "
          f"(cells show ladder net minus baseline net, in Rs.'000)")
    print()
    header = "stress \\ cost  " + "".join(f"{c:>9.0f}" for c in sweep["cost_grid"])
    print(header)
    print("-" * len(header))
    for s in sweep["stress_grid"]:
        row = [c for c in sweep["cells"] if c["stress"] == s]
        row.sort(key=lambda c: c["voice_cost"])
        cells = "".join(
            f"{('+' if c['ladder_minus_baseline'] >= 0 else '') + format(c['ladder_minus_baseline'] / 1000, '.0f'):>9}"
            for c in row)
        print(f"{s:>11.1f}  {cells}")
    print("-" * len(header))
    x = sweep["crossover"]
    print(f"ladder beats {sweep['baseline']} in "
          f"{sum(1 for c in sweep['cells'] if c['ladder_wins'])}/{len(sweep['cells'])} cells")
    print(f"  at cost Rs.12: ladder wins down to stress = "
          f"{x['min_stress_ladder_wins_at_cost_12']}")
    print(f"  at stress 1.0: ladder wins up to voice cost = Rs."
          f"{x['max_cost_ladder_wins_at_stress_1']}")
    bad = [c for c in sweep["cells"] if c["violations"]]
    print(f"  guardrail violations across the whole sweep: "
          f"{sum(c['violations'] for c in sweep['cells'])}"
          + (f"  !! in {len(bad)} cells" if bad else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m recovery.sweep")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--voice-budget", type=float, default=DEFAULT_VOICE_BUDGET)
    ap.add_argument("--human-cap", type=int, default=DEFAULT_HUMAN_CAP)
    ap.add_argument("--out", default="audit/sweep.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    sweep = run_sweep(args.seed, args.voice_budget, args.human_cap)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(sweep, fh, indent=2)

    if args.json:
        json.dump(sweep, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print(sweep)
        print(f"\nfull grid written to {os.path.abspath(args.out)}")
    # non-zero only on a real failure: a guardrail leak somewhere in the grid
    return 1 if sum(c["violations"] for c in sweep["cells"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
