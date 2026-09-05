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
from .analysis import run_all
from .assumptions import header as assumptions_header
from .guardrails import DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET


def _print_table(rows):
    cols = [
        ("policy", 18, "<", lambda r: r["policy"]),
        ("recovered", 14, ">", lambda r: f"{r['recovered']:,.0f}"),
        ("spend", 10, ">", lambda r: f"{r['spend']:,.0f}"),
        ("net", 14, ">", lambda r: f"{r['net']:,.0f}"),
        ("rec-%", 8, ">", lambda r: f"{r['rate'] * 100:.1f}%"),
        ("acct-%", 8, ">", lambda r: f"{r['acct_rate'] * 100:.1f}%"),
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
    print("all rupee figures are synthetic; rec-% = rupees recovered / rupees at risk; "
          "acct-% = accounts recovered / accounts; 'viol' MUST be 0")


def _print_concentration(rows, events_by_policy, ledger):
    by_id = {a.account_id: a for a in ledger}
    total_at_risk = sum(a.amount for a in ledger)
    rowmap = {r["policy"]: r for r in rows}

    # per-account recovered, for the ladder
    rec = {}
    for ev in events_by_policy["ladder"]:
        if ev.get("event") == "episode_end":
            rec[ev["account_id"]] = ev.get("recovered_amount", 0.0) or 0.0
    ladder_total = sum(rec.values()) or 1.0
    top = sorted(rec.values(), reverse=True)
    top_amt = sorted(((v, k) for k, v in rec.items()), reverse=True)

    print("\n-- concentration --------------------------------------------------")
    for n in (1, 5, 10):
        share_rec = sum(top[:n]) / ladder_total * 100
        share_book = sum(a.amount for a in
                         sorted(ledger, key=lambda a: -a.amount)[:n]) / total_at_risk * 100
        print(f"  top {n:>2} accounts: {share_rec:5.1f}% of what the ladder recovered  "
              f"| {share_book:5.1f}% of the book's rupees at risk")
    print(f"  the single biggest recovered account: "
          f"{top_amt[0][1]} ({by_id[top_amt[0][1]].reason}), Rs.{top_amt[0][0]:,.0f}")

    print("\n-- recovered by failure class (recovered rupees / at-risk rupees) -")
    print(f"  {'policy':<18}{'TRANSIENT (self-heals)':>28}{'ACTION_REQUIRED':>22}")
    for name in ("ladder", "standard_playbook", "retry_only"):
        c = rowmap[name]["by_class"]
        t, a = c["transient"], c["action_required"]
        print(f"  {name:<18}"
              f"{('Rs.' + format(t['recovered'], ',.0f') + f'  ({t['rate']*100:.0f}%)'):>28}"
              f"{('Rs.' + format(a['recovered'], ',.0f') + f'  ({a['rate']*100:.0f}%)'):>22}")

    print("\n-- the real test: ACTION_REQUIRED accounts only -----------------")
    la = rowmap["ladder"]["by_class"]["action_required"]
    sa = rowmap["standard_playbook"]["by_class"]["action_required"]
    ra = rowmap["retry_only"]["by_class"]["action_required"]
    print(f"  accounts in class: {la['accounts']}  |  at risk: Rs.{la['at_risk']:,.0f}")
    for name, s in (("ladder", la), ("standard_playbook", sa), ("retry_only", ra)):
        print(f"  {name:<18} recovered Rs.{s['recovered']:>12,.0f}  "
              f"spend Rs.{s['spend']:>8,.0f}  net Rs.{s['net']:>12,.0f}  "
              f"({s['rate']*100:.0f}% of rupees, {s['n_recovered']}/{s['accounts']} accts)")
    print(f"  ladder net minus standard_playbook net on this class: "
          f"Rs.{la['net'] - sa['net']:,.0f}")


def _print_cohorts(rows):
    for r in rows:
        print(f"\n{r['policy']}")
        for seg in ("B2C", "B2B"):
            s = r["by_segment"][seg]
            cpr = "-" if s["cost_per_100"] is None else f"Rs.{s['cost_per_100']:.2f}/100"
            print(f"  {seg:<4} {s['accounts']:>3} acc  "
                  f"net Rs.{s['net']:>12,.0f}  rate {s['rate'] * 100:>4.1f}%  {cpr}")
        worst = sorted(r["by_reason"].items(),
                       key=lambda kv: kv[1]["net"])[:3]
        tail = ", ".join(f"{k} (net Rs.{v['net']:,.0f})" for k, v in worst if v["accounts"])
        if tail:
            print(f"  weakest reasons: {tail}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m recovery")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--stress", type=float, default=1.0,
                    help="scale voice lift only (sensitivity knob); e.g. 0.5, 1.5")
    ap.add_argument("--sigma", type=float, default=0.0,
                    help="lognormal noise on the AGENT's probability estimate "
                         "(world still resolves on truth); e.g. 0.35, 0.6")
    ap.add_argument("--voice-budget", type=float, default=DEFAULT_VOICE_BUDGET,
                    help="rupees of voice spend allowed across the whole run")
    ap.add_argument("--human-cap", type=int, default=DEFAULT_HUMAN_CAP,
                    help="manual human escalations allowed across the whole run")
    ap.add_argument("--audit-dir", default="audit",
                    help="directory for per-policy JSONL trails")
    ap.add_argument("--cohorts", action="store_true",
                    help="also print the B2B/B2C and by-reason breakdown")
    ap.add_argument("--concentration", action="store_true",
                    help="print value concentration + transient vs action-required split")
    ap.add_argument("--json", action="store_true",
                    help="emit the full scorecard (incl. cohorts) as JSON")
    args = ap.parse_args(argv)

    if not args.json:
        print(world.ASSUMPTION_BANNER)
        print(assumptions_header())
        print(f"voice_call cost Rs.{core.INTERVENTION_COST['voice_call']:.2f} "
              f"(derived, recovery/costs.py)")
        if args.sigma > 0:
            print(f"estimate noise: sigma={args.sigma} "
                  f"(agent scores on a noised p_hat; world resolves on p)")
        else:
            print("estimate noise: sigma=0 -- agent scores on the ground-truth model")
        print()

    result = run_all(args.seed, args.stress, args.voice_budget, args.human_cap,
                     keep_events=True, sigma=args.sigma)
    lsum = result["ledger"]
    rows = result["policies"]

    # write the JSONL audit trails
    ledger = core.build_ledger(args.seed)
    os.makedirs(args.audit_dir, exist_ok=True)
    for name, events in result["events"].items():
        with open(os.path.join(args.audit_dir, f"{name}.jsonl"), "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    if args.json:
        from .assumptions import counts as _acounts
        payload = {"ledger": lsum, "run": vars(args), "policies": rows,
                   "total_violations": result["total_violations"],
                   "assumptions": _acounts(),
                   "voice_call_cost": core.INTERVENTION_COST["voice_call"]}
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1 if result["total_violations"] else 0

    print(f"ledger: {lsum['accounts']} accounts, "
          f"Rs.{lsum['at_risk_rupees']:,.0f} at risk, "
          f"{lsum['b2b_accounts']} B2B ({lsum['b2b_share_of_rupees'] * 100:.0f}% of rupees), "
          f"{lsum['dnc_accounts']} on DNC, "
          f"{lsum['with_open_ptp']} with an open promise-to-pay")
    print(f"run: seed={args.seed}  stress={args.stress}  "
          f"voice_budget=Rs.{args.voice_budget:,.0f}  human_cap={args.human_cap}")
    print()
    _print_table(rows)
    if args.cohorts:
        _print_cohorts(rows)
    if args.concentration:
        _print_concentration(rows, result["events"], core.build_ledger(args.seed))
    print()
    best = max(rows, key=lambda r: r["net"])
    print(f"best net: {best['policy']}  (Rs.{best['net']:,.0f})")
    print(f"audit trails written to {os.path.abspath(args.audit_dir)}/<policy>.jsonl")
    for r in rows:
        if r["violation_breakdown"]:
            print(f"  !! {r['policy']} violations: {r['violation_breakdown']}")
    return 1 if result["total_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
