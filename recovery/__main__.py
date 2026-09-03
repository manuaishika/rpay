"""python -m recovery -- run every policy over the synthetic ledger and print
the scorecard: recovered / spend / net / rate / calls / PTPs / cost-per-Rs.100,
plus guardrail violations (which must be 0 for every policy).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import core, world
from .guardrails import DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET, audit_executed
from .ladder import POLICIES, run_policy


def _metrics(name, episodes, events, violations):
    n = len(episodes)
    recovered = sum(e.recovered_amount for e in episodes)
    spend = sum(e.spend for e in episodes)
    n_recovered = sum(1 for e in episodes if e.recovered)
    calls = sum(1 for ev in events
                if ev.get("event") == "action" and ev.get("intervention") == "voice_call")
    ptps = sum(1 for ev in events if ev.get("event") == "ptp_created")
    ptps_paid = sum(1 for e in episodes if e.ptp_paid)
    cpr = (spend / recovered * 100.0) if recovered > 0 else None
    return {
        "policy": name,
        "recovered": round(recovered, 2),
        "spend": round(spend, 2),
        "net": round(recovered - spend, 2),
        "rate": round(n_recovered / n, 4),
        "n_recovered": n_recovered,
        "calls": calls,
        "ptps": ptps,
        "ptps_paid": ptps_paid,
        "cost_per_100": round(cpr, 3) if cpr is not None else None,
        "violations": len(violations),
        "violation_breakdown": _count(violations),
    }


def _count(violations):
    out = {}
    for v in violations:
        out[v["rule"]] = out.get(v["rule"], 0) + 1
    return out


def _print_table(rows):
    cols = [
        ("policy", 18, "<", lambda r: r["policy"]),
        ("recovered", 14, ">", lambda r: f"{r['recovered']:,.0f}"),
        ("spend", 10, ">", lambda r: f"{r['spend']:,.0f}"),
        ("net", 14, ">", lambda r: f"{r['net']:,.0f}"),
        ("rate", 8, ">", lambda r: f"{r['rate'] * 100:.1f}%"),
        ("calls", 7, ">", lambda r: str(r["calls"])),
        ("PTP", 6, ">", lambda r: str(r["ptps"])),
        ("Rs/100", 9, ">", lambda r: "-" if r["cost_per_100"] is None else f"{r['cost_per_100']:.2f}"),
        ("viol", 6, ">", lambda r: str(r["violations"])),
    ]
    header = "".join(f"{name:{align}{width}}" for name, width, align, _ in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("".join(f"{fn(r):{align}{width}}" for _, width, align, fn in cols))
    print("-" * len(header))
    print("all rupee figures are synthetic; 'Rs/100' = spend to recover Rs.100; "
          "'viol' MUST be 0")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m recovery")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--stress", type=float, default=1.0,
                    help="scale voice lift only (sensitivity knob); e.g. 0.5, 1.5")
    ap.add_argument("--voice-budget", type=float, default=DEFAULT_VOICE_BUDGET,
                    help="rupees of voice spend allowed across the whole run")
    ap.add_argument("--human-cap", type=int, default=DEFAULT_HUMAN_CAP,
                    help="manual human escalations allowed across the whole run")
    ap.add_argument("--audit-dir", default="audit",
                    help="directory for per-policy JSONL trails")
    ap.add_argument("--json", action="store_true",
                    help="emit the scorecard as JSON instead of a table")
    args = ap.parse_args(argv)

    if not args.json:
        print(world.ASSUMPTION_BANNER)
        print()

    ledger = core.build_ledger(args.seed)
    lsum = core.ledger_summary(ledger)
    if not args.json:
        print(f"ledger: {lsum['accounts']} accounts, "
              f"Rs.{lsum['at_risk_rupees']:,.0f} at risk, "
              f"{lsum['b2b_accounts']} B2B ({lsum['b2b_share_of_rupees'] * 100:.0f}% of rupees), "
              f"{lsum['dnc_accounts']} on DNC, "
              f"{lsum['with_open_ptp']} with an open promise-to-pay")
        print(f"run: seed={args.seed}  stress={args.stress}  "
              f"voice_budget=Rs.{args.voice_budget:,.0f}")
        print()

    os.makedirs(args.audit_dir, exist_ok=True)
    rows = []
    total_violations = 0
    for name in POLICIES:
        episodes, events = run_policy(name, ledger, args.seed, args.stress,
                                     args.voice_budget, args.human_cap)
        path = os.path.join(args.audit_dir, f"{name}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        violations = audit_executed(events, ledger, args.voice_budget, args.human_cap)
        total_violations += len(violations)
        rows.append(_metrics(name, episodes, events, violations))

    if args.json:
        json.dump({"ledger": lsum, "run": vars(args), "policies": rows},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_table(rows)
        print()
        best = max(rows, key=lambda r: r["net"])
        print(f"best net: {best['policy']}  (Rs.{best['net']:,.0f})")
        print(f"audit trails written to {os.path.abspath(args.audit_dir)}/<policy>.jsonl")
        for r in rows:
            if r["violation_breakdown"]:
                print(f"  !! {r['policy']} violations: {r['violation_breakdown']}")

    return 1 if total_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
