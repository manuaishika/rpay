"""dashboard.py -- assemble everything the HTML scorecard needs into one JSON.

    python -m recovery.dashboard            # writes audit/dashboard.json

The HTML artifact bakes this file in, so the page is a static snapshot of a
real run (no server, no live Python).
"""
from __future__ import annotations

import argparse
import json
import os

from . import core, world
from .analysis import run_all
from .guardrails import (
    DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET, CONTACT_CAP_7D, VOICE_ATTEMPTS_MAX,
    WINDOW_START, WINDOW_END, VIOLATION_CODES,
)
from .ladder import run_policy
from .sweep import run_sweep

TRACE_KEYS = ("event", "stage", "intervention", "cost", "outcome",
              "expected_net", "p_now", "p_ptp", "cause", "rule", "best_net",
              "result", "recovered", "recovered_via", "spend", "due")


def _collapse(evs: list) -> list:
    """Fold a run of blocked-by-same-rule events at one stage into a single row."""
    out: list = []
    for ev in evs:
        if (ev.get("event") == "blocked" and out
                and out[-1].get("event") == "blocked"
                and out[-1].get("stage") == ev.get("stage")
                and out[-1].get("rule") == ev.get("rule")):
            out[-1]["channels"] += ", " + ev["intervention"]
        elif ev.get("event") == "blocked":
            out.append({"event": "blocked", "stage": ev.get("stage"),
                        "rule": ev.get("rule"), "channels": ev["intervention"]})
        else:
            out.append(ev)
    return out


def _traces(seed, stress, voice_budget, human_cap, want=7):
    ledger = core.build_ledger(seed)
    by_id = {a.account_id: a for a in ledger}
    _, events = run_policy("ladder", ledger, seed, stress, voice_budget, human_cap)
    per_acc: dict[str, list] = {}
    for ev in events:
        per_acc.setdefault(ev["account_id"], []).append(
            {k: ev[k] for k in TRACE_KEYS if k in ev})
    per_acc = {aid: _collapse(evs) for aid, evs in per_acc.items()}

    def pick(pred, seen):
        for aid, evs in per_acc.items():
            if aid not in seen and pred(by_id[aid], evs):
                return aid

    wanted = [
        ("big B2B recovered by a call",
         lambda a, e: a.segment == "B2B" and a.amount > 40000
         and any(x.get("outcome") == "recovered" and x.get("intervention") == "voice_call" for x in e)),
        ("promise-to-pay captured on a connected call",
         lambda a, e: any(x["event"] == "ptp_created" for x in e)),
        ("small B2C the agent declined to chase",
         lambda a, e: a.segment == "B2C" and a.amount < 800
         and any(x["event"] == "stop" for x in e)),
        ("revoked mandate -- retry scores 0.0",
         lambda a, e: a.reason == "mandate_revoked"),
        ("abandoned checkout -- link, not a call",
         lambda a, e: a.reason == "checkout_abandoned"),
        ("blocked by a guardrail mid-ladder",
         lambda a, e: any(x["event"] == "blocked" for x in e)),
        ("recovered by a cheap nudge, no call needed",
         lambda a, e: any(x.get("outcome") == "recovered"
                          and x.get("intervention") in ("sms_link", "whatsapp_nudge") for x in e)),
    ]
    out = []
    seen = set()
    for label, pred in wanted:
        aid = pick(pred, seen)
        if aid:
            seen.add(aid)
            a = by_id[aid]
            out.append({
                "label": label,
                "account_id": aid,
                "segment": a.segment,
                "reason": a.reason,
                "amount": round(a.amount, 2),
                "prior_failures": a.prior_failures,
                "language": a.language,
                "events": per_acc[aid],
            })
        if len(out) >= want:
            break
    return out


def _sample_calls(calls_dir="audit/calls"):
    out = []
    if not os.path.isdir(calls_dir):
        return out
    for fn in sorted(os.listdir(calls_dir)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(calls_dir, fn), encoding="utf-8") as fh:
            m = json.load(fh)
        out.append({k: m[k] for k in
                    ("account_id", "segment", "reason", "amount", "language",
                     "speaker", "script") if k in m})
    return out


def build(seed=20260903, stress=1.0, voice_budget=DEFAULT_VOICE_BUDGET,
          human_cap=DEFAULT_HUMAN_CAP) -> dict:
    main = run_all(seed, stress, voice_budget, human_cap)
    sweep = run_sweep(seed, voice_budget, human_cap)
    return {
        "meta": {
            "seed": seed, "stress": stress, "voice_budget": voice_budget,
            "human_cap": human_cap,
            "voice_cost": core.DEFAULT_VOICE_COST,
            "voice_pickup_rate": world.VOICE_PICKUP_RATE,
            "generated_note": "all figures synthetic; priors are assumptions (see world.py)",
        },
        "rules": {
            "contact_window": [WINDOW_START.strftime("%H:%M"), WINDOW_END.strftime("%H:%M")],
            "contact_cap_7d": CONTACT_CAP_7D,
            "voice_attempts_max": VOICE_ATTEMPTS_MAX,
            "codes": list(VIOLATION_CODES),
        },
        "interventions": dict(core.INTERVENTION_COST),
        "taxonomy": {
            "transient": sorted(core.TRANSIENT),
            "action_required": sorted(core.ACTION_REQUIRED),
        },
        "ledger": main["ledger"],
        "policies": main["policies"],
        "total_violations": main["total_violations"],
        "sweep": sweep,
        "traces": _traces(seed, stress, voice_budget, human_cap),
        "sample_calls": _sample_calls(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m recovery.dashboard")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--out", default="audit/dashboard.json")
    args = ap.parse_args(argv)
    data = build(args.seed)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}  "
          f"({len(data['policies'])} policies, {len(data['sweep']['cells'])} sweep cells, "
          f"{len(data['traces'])} traces, {len(data['sample_calls'])} call scripts, "
          f"violations={data['total_violations']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
