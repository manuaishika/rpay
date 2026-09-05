"""sweep.py -- the thesis stress-test.

The argument rests on two estimated numbers: the voice LIFT (how much a call
actually helps -- world.py) and the voice COST (derived in recovery/costs.py,
~Rs.5.44). This sweeps both, runs every policy in every cell, reports where
the sequential agent stops beating the best fixed playbook, and -- for a set
of representative accounts -- the voice cost at which the agent stops
preferring a call.

    python -m recovery.sweep                 # table + audit/sweep.json
    python -m recovery.sweep --json          # just the JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import core, world
from .analysis import run_all
from .assumptions import header as assumptions_header
from .guardrails import DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET

DERIVED_COST = round(core.DEFAULT_VOICE_COST, 2)   # ~Rs.5.44 from recovery/costs.py
STRESS_GRID = [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6]
COST_GRID = sorted({3.0, 4.0, 5.0, DERIVED_COST, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0})
BASELINE = "standard_playbook"          # best fixed playbook to beat

# representative (reason, amount) cases for the decision-boundary readout
BOUNDARY_CASES = [
    ("insufficient_funds", 500.0), ("insufficient_funds", 2000.0),
    ("insufficient_funds", 10000.0), ("card_expired", 1500.0),
    ("invoice_overdue", 6000.0), ("checkout_abandoned", 1500.0),
    ("mandate_revoked", 40000.0),
]


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


def _mk_acc(reason, amount):
    return core.Account(
        account_id="boundary", segment="B2B" if amount >= 20000 else "B2C",
        reason=reason, amount=amount, tenure_months=12, prior_failures=0,
        language="hinglish", dnc=False, contacts_last_7d=0, has_phone=True)


def decision_boundary(stress: float = 1.0) -> list[dict]:
    """For representative accounts: the voice cost at which the agent's stage-1
    pick flips away from voice_call (below the best non-voice option, or below 0)."""
    from .ladder import _expected_net, Episode
    nonvoice = ["silent_retry", "sms_link", "whatsapp_nudge", "human_escalation"]
    out = []
    for reason, amount in BOUNDARY_CASES:
        acc = _mk_acc(reason, amount)
        ep = Episode(account_id=acc.account_id, policy="boundary")
        best_alt = max(_expected_net(reason, iv, acc, ep, stress)[0] for iv in nonvoice)
        threshold = max(best_alt, 0.0)

        flip, c = None, 1.0
        while c <= 30.0:
            with core.voice_cost(c):
                if _expected_net(reason, "voice_call", acc, ep, stress)[0] < threshold:
                    flip = round(c, 2)
                    break
            c += 0.25
        with core.voice_cost(DERIVED_COST):
            v_now = _expected_net(reason, "voice_call", acc, ep, stress)[0]
        out.append({
            "reason": reason, "amount": amount,
            "best_nonvoice_net": round(best_alt, 2),
            "voice_net_at_derived_cost": round(v_now, 2),
            "prefers_voice_at_derived_cost": v_now >= threshold,
            "voice_cost_where_it_flips": flip,
        })
    return out


def run_sweep(seed=20260903, voice_budget=DEFAULT_VOICE_BUDGET, human_cap=DEFAULT_HUMAN_CAP,
              stress_grid=None, cost_grid=None) -> dict:
    stress_grid = stress_grid or STRESS_GRID
    cost_grid = cost_grid or COST_GRID
    cells = [_cell(seed, s, c, voice_budget, human_cap)
             for s in stress_grid for c in cost_grid]

    def _floor_at(cost):
        col = sorted((c for c in cells if c["voice_cost"] == cost), key=lambda c: c["stress"])
        return next((c["stress"] for c in col if c["ladder_wins"]), None)

    at_stress1 = sorted((c for c in cells if c["stress"] == 1.0), key=lambda c: c["voice_cost"])
    cost_ceiling = next((c["voice_cost"] for c in reversed(at_stress1) if c["ladder_wins"]), None)

    return {
        "seed": seed,
        "voice_budget": voice_budget,
        "human_cap": human_cap,
        "baseline": BASELINE,
        "derived_voice_cost": DERIVED_COST,
        "stress_grid": stress_grid,
        "cost_grid": cost_grid,
        "cells": cells,
        "crossover": {
            "min_stress_ladder_wins_at_derived_cost": _floor_at(DERIVED_COST),
            "min_stress_ladder_wins_at_cost_12": _floor_at(12.0),
            "max_cost_ladder_wins_at_stress_1": cost_ceiling,
            "ladder_wins_everywhere": all(c["ladder_wins"] for c in cells),
        },
        "decision_boundary": decision_boundary(1.0),
    }


def _print(sweep: dict):
    print(f"sweep: seed={sweep['seed']}  baseline={sweep['baseline']}  "
          f"(cells show ladder net minus baseline net, in Rs.'000)")
    print()
    header = "stress \\ cost  " + "".join(f"{c:>9g}" for c in sweep["cost_grid"])
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
    print(f"  at the derived cost Rs.{sweep['derived_voice_cost']}: ladder wins down to stress = "
          f"{x['min_stress_ladder_wins_at_derived_cost']}")
    print(f"  at cost Rs.12: ladder wins down to stress = "
          f"{x['min_stress_ladder_wins_at_cost_12']}")
    print(f"  at stress 1.0: ladder wins up to voice cost = Rs."
          f"{x['max_cost_ladder_wins_at_stress_1']}")
    bad = [c for c in sweep["cells"] if c["violations"]]
    print(f"  guardrail violations across the whole sweep: "
          f"{sum(c['violations'] for c in sweep['cells'])}"
          + (f"  !! in {len(bad)} cells" if bad else ""))

    d = sweep["derived_voice_cost"]
    print(f"\ndecision boundary -- at what voice cost the stage-1 pick leaves voice"
          f"  (derived cost Rs.{d}):")
    print(f"  {'reason':<20}{'amount':>12}{'best non-voice net':>20}"
          f"{'voice now?':>12}{'flips at':>12}")
    for b in sweep["decision_boundary"]:
        flip = f"Rs.{b['voice_cost_where_it_flips']}" if b["voice_cost_where_it_flips"] else ">Rs.30"
        pref = "yes" if b["prefers_voice_at_derived_cost"] else "no"
        print(f"  {b['reason']:<20}{('Rs.' + format(b['amount'], ',.0f')):>12}"
              f"{('Rs.' + format(b['best_nonvoice_net'], ',.0f')):>20}{pref:>12}{flip:>12}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m recovery.sweep")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--voice-budget", type=float, default=DEFAULT_VOICE_BUDGET)
    ap.add_argument("--human-cap", type=int, default=DEFAULT_HUMAN_CAP)
    ap.add_argument("--out", default="audit/sweep.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not args.json:
        print(assumptions_header())
        print()
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
