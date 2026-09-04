"""analysis.py -- shared scoring / slicing used by __main__, sweep, and the
dashboard data dump. No I/O, no argparse.
"""
from __future__ import annotations

from . import core
from .guardrails import audit_executed
from .ladder import POLICIES, run_policy


def count_rules(violations) -> dict:
    out: dict[str, int] = {}
    for v in violations:
        out[v["rule"]] = out.get(v["rule"], 0) + 1
    return out


def _slice(episodes) -> dict:
    n = len(episodes)
    if n == 0:
        return {"accounts": 0, "recovered": 0.0, "spend": 0.0, "net": 0.0,
                "rate": 0.0, "n_recovered": 0, "cost_per_100": None}
    recovered = sum(e.recovered_amount for e in episodes)
    spend = sum(e.spend for e in episodes)
    n_rec = sum(1 for e in episodes if e.recovered)
    return {
        "accounts": n,
        "recovered": round(recovered, 2),
        "spend": round(spend, 2),
        "net": round(recovered - spend, 2),
        "rate": round(n_rec / n, 4),
        "n_recovered": n_rec,
        "cost_per_100": round(spend / recovered * 100.0, 3) if recovered > 0 else None,
    }


def policy_metrics(name, episodes, events, violations, ledger) -> dict:
    by_id = {a.account_id: a for a in ledger}
    calls = sum(1 for ev in events
                if ev.get("event") == "action" and ev.get("intervention") == "voice_call")
    ptps = sum(1 for ev in events if ev.get("event") == "ptp_created")
    stops = sum(1 for ev in events if ev.get("event") == "stop")

    m = {"policy": name, **_slice(episodes)}
    m.update({
        "calls": calls,
        "ptps": ptps,
        "ptps_paid": sum(1 for e in episodes if e.ptp_paid),
        "stops": stops,
        "violations": len(violations),
        "violation_breakdown": count_rules(violations),
        "by_segment": {
            seg: _slice([e for e in episodes if by_id[e.account_id].segment == seg])
            for seg in ("B2C", "B2B")
        },
        "by_reason": {
            reason: _slice([e for e in episodes if by_id[e.account_id].reason == reason])
            for reason in core.FAILURE_REASONS
        },
    })
    return m


def run_all(seed: int, stress: float, voice_budget: float, human_cap: int,
            keep_events: bool = False) -> dict:
    """Run every policy over one ledger. Returns metrics (+ raw events if asked)."""
    ledger = core.build_ledger(seed)
    out = {"ledger": core.ledger_summary(ledger), "policies": [], "events": {}}
    for name in POLICIES:
        episodes, events = run_policy(name, ledger, seed, stress, voice_budget, human_cap)
        violations = audit_executed(events, ledger, voice_budget, human_cap)
        out["policies"].append(policy_metrics(name, episodes, events, violations, ledger))
        if keep_events:
            out["events"][name] = events
    out["total_violations"] = sum(p["violations"] for p in out["policies"])
    return out
