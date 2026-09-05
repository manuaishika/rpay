"""ladder.py -- the bounded sequential agent, plus fixed-playbook baselines.

The agent runs up to 5 stages per failed payment. At each stage it:

  1. asks guardrails.check() which channels it is ALLOWED to use right now,
  2. scores each allowed channel by expected NET rupees:
         E[net] = p_recover * amount - cost
     where p_recover is decayed by
        - channel fatigue: 0.72 for every prior use of THAT channel, and
        - evidence decay:  0.88 for every attempt that already failed here
          (a failure is information -- the money is probably not there),
  3. gives voice an extra OPTION VALUE term: a call that connects but does
     not convert can still capture a dated promise-to-pay, which pays later
     at zero marginal spend,
  4. plays the highest-scoring action IF its expected net is > 0, else STOPS.

Stopping the moment nothing clears zero in expectation is the entire point:
it is why the agent will not spend Rs.5+ chasing a Rs.90 B2C invoice with a
revoked mandate. The fixed playbooks below do not stop -- they just run
their script until it recovers, runs out, or the gate blocks everything.

Every policy emits the same JSONL event schema and is audited identically.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta

from . import core, world
from .guardrails import Guardrails

MAX_STAGES = 5
CHANNEL_FATIGUE = 0.72       # per repeat of the same channel within an episode
EVIDENCE_DECAY = 0.88        # per attempt already failed within an episode
CHRONIC_PENALTY = 0.92       # per lifetime prior failure (capped at 5)


@dataclass
class Episode:
    account_id: str
    policy: str
    total_attempts: int = 0
    attempts_by_channel: dict = field(default_factory=dict)
    spend: float = 0.0
    recovered: bool = False
    recovered_amount: float = 0.0
    recovered_via: str | None = None      # channel, or "promise_to_pay"
    ptp_due: str | None = None
    ptp_paid: bool = False
    stopped_reason: str | None = None


# --------------------------------------------------------------------------
# shared probability / scoring model
# --------------------------------------------------------------------------

def _channel_p(reason: str, iv: str, account, ep: Episode, stress: float) -> float:
    """Fatigue- and evidence-adjusted p. For voice this is convert|connect."""
    p = world.p_recover(reason, iv, stress)
    if p <= 0.0:
        return 0.0
    p *= CHANNEL_FATIGUE ** ep.attempts_by_channel.get(iv, 0)
    p *= EVIDENCE_DECAY ** ep.total_attempts
    if iv != "voice_call":
        p *= CHRONIC_PENALTY ** min(account.prior_failures, 5)
    return max(0.0, min(1.0, p))


def _expected_net(reason: str, iv: str, account, ep: Episode, stress: float):
    cost = core.INTERVENTION_COST[iv]
    p = _channel_p(reason, iv, account, ep, stress)
    amt = account.amount
    if iv == "voice_call":
        pickup = world.VOICE_PICKUP_RATE
        p_now = pickup * p
        p_ptp = pickup * (1.0 - p) * world.PTP_CAPTURE_ON_CONNECT
        option_value = p_ptp * world.PTP_CONVERSION * amt
        net = p_now * amt + option_value - cost
        return net, {"p_now": round(p_now, 4), "p_ptp": round(p_ptp, 4),
                     "option_value": round(option_value, 2)}
    return p * amt - cost, {"p_now": round(p, 4)}


def _simulate(rng: random.Random, reason: str, iv: str, account, ep: Episode,
              stress: float) -> str:
    """Draw an outcome: 'recovered' | 'ptp' | 'no_pickup' | 'failed'."""
    p = _channel_p(reason, iv, account, ep, stress)
    if iv == "voice_call":
        if rng.random() >= world.VOICE_PICKUP_RATE:
            return "no_pickup"
        if rng.random() < p:
            return "recovered"
        if rng.random() < world.PTP_CAPTURE_ON_CONNECT:
            return "ptp"
        return "failed"
    return "recovered" if rng.random() < p else "failed"


# --------------------------------------------------------------------------
# event helpers / time model
# --------------------------------------------------------------------------

def _ev(account, event: str, now, **kw) -> dict:
    d = {
        "event": event,
        "account_id": account.account_id,
        "ts": now.isoformat(),
        "reason": account.reason,
        "segment": account.segment,
        "amount": round(account.amount, 2),
    }
    d.update(kw)
    return d


def _start(account, now0):
    """Per-account first-contact time. Most accounts land inside the 09:00-19:00
    window; roughly one in nine is deliberately parked at 06:00 so the window
    gate is genuinely exercised (those accounts can only silent_retry or stop)."""
    i = int(account.account_id.split("_")[1])
    hour = 6 if i % 9 == 0 else 9 + (i % 9)
    return now0.replace(hour=hour, minute=0, second=0, microsecond=0)


def _advance(now):
    """One stage per day, same local hour -- so a stage that keeps contacting
    the same account runs into the 3-contacts / 7-days cap by stage four."""
    return now + timedelta(days=1)


def _resolve_ptp(account, ep: Episode, events: list, rng: random.Random, now) -> None:
    if ep.ptp_due and not ep.recovered:
        paid = rng.random() < world.PTP_CONVERSION
        events.append(_ev(account, "ptp_resolved", now,
                          result="paid" if paid else "broken", due=ep.ptp_due))
        if paid:
            ep.recovered = True
            ep.recovered_amount = account.amount
            ep.recovered_via = "promise_to_pay"
            ep.ptp_paid = True


def _play(account, iv, now, stage, ep, guard, rng, stress, events, extra):
    """Execute one intervention, record it, return the outcome string."""
    cost = core.INTERVENTION_COST[iv]
    outcome = _simulate(rng, account.reason, iv, account, ep, stress)
    guard.commit(account, iv, now, cost)
    ep.spend += cost
    ep.total_attempts += 1
    ep.attempts_by_channel[iv] = ep.attempts_by_channel.get(iv, 0) + 1
    rec = dict(stage=stage, intervention=iv, cost=cost, outcome=outcome)
    rec.update(extra)
    events.append(_ev(account, "action", now, **rec))
    if outcome == "recovered":
        ep.recovered = True
        ep.recovered_amount = account.amount
        ep.recovered_via = iv
    elif outcome == "ptp":
        due = (now + timedelta(days=world.PROMISE_HORIZON_DAYS)).date()
        guard.register_ptp(account.account_id, due)
        ep.ptp_due = due.isoformat()
        events.append(_ev(account, "ptp_created", now, stage=stage, due=ep.ptp_due))
    return outcome


# --------------------------------------------------------------------------
# policy 1: the bounded sequential agent
# --------------------------------------------------------------------------

def policy_ladder(account, guard: Guardrails, rng: random.Random, stress: float, now0):
    now = _start(account, now0)
    ep = Episode(account_id=account.account_id, policy="ladder")
    events = [_ev(account, "episode_start", now, policy="ladder")]

    for stage in range(1, MAX_STAGES + 1):
        scored = []
        for iv in core.INTERVENTION_COST:
            ok, code = guard.check(account, iv, now)
            if not ok:
                events.append(_ev(account, "blocked", now, stage=stage,
                                  intervention=iv, rule=code))
                continue
            net, meta = _expected_net(account.reason, iv, account, ep, stress)
            scored.append((net, iv, meta))

        if not scored:
            ep.stopped_reason = "no_compliant_channel"
            events.append(_ev(account, "stop", now, stage=stage,
                              cause="no_compliant_channel"))
            break

        scored.sort(key=lambda t: t[0], reverse=True)
        best_net, iv, meta = scored[0]
        if best_net <= 0.0:
            ep.stopped_reason = "expected_net_not_positive"
            events.append(_ev(account, "stop", now, stage=stage,
                              cause="expected_net<=0", best_channel=iv,
                              best_net=round(best_net, 2)))
            break

        outcome = _play(account, iv, now, stage, ep, guard, rng, stress, events,
                        extra={"expected_net": round(best_net, 2), **meta})
        if outcome in ("recovered", "ptp"):
            break
        now = _advance(now)
    else:
        ep.stopped_reason = "max_stages"

    _resolve_ptp(account, ep, events, rng, now)
    events.append(_ev(account, "episode_end", now, recovered=ep.recovered,
                      recovered_via=ep.recovered_via,
                      recovered_amount=round(ep.recovered_amount, 2),
                      spend=round(ep.spend, 2),
                      stopped_reason=ep.stopped_reason))
    return ep, events


# --------------------------------------------------------------------------
# policies 2-5: fixed playbooks (same gate, same simulator, no stop rule)
# --------------------------------------------------------------------------

def _make_playbook(name: str, sequence: list[str]):
    def run(account, guard: Guardrails, rng: random.Random, stress: float, now0):
        now = _start(account, now0)
        ep = Episode(account_id=account.account_id, policy=name)
        events = [_ev(account, "episode_start", now, policy=name)]
        stage = 0
        for iv in sequence:
            stage += 1
            if stage > MAX_STAGES:
                break
            ok, code = guard.check(account, iv, now)
            if not ok:
                events.append(_ev(account, "blocked", now, stage=stage,
                                  intervention=iv, rule=code))
                continue
            outcome = _play(account, iv, now, stage, ep, guard, rng, stress,
                            events, extra={})
            if outcome in ("recovered", "ptp"):
                break
            now = _advance(now)
        ep.stopped_reason = "playbook_exhausted"
        _resolve_ptp(account, ep, events, rng, now)
        events.append(_ev(account, "episode_end", now, recovered=ep.recovered,
                          recovered_via=ep.recovered_via,
                          recovered_amount=round(ep.recovered_amount, 2),
                          spend=round(ep.spend, 2),
                          stopped_reason=ep.stopped_reason))
        return ep, events
    return run


POLICIES = {
    "ladder": policy_ladder,
    "retry_only": _make_playbook("retry_only", ["silent_retry", "silent_retry", "silent_retry"]),
    "nudge_ladder": _make_playbook("nudge_ladder",
                                   ["sms_link", "whatsapp_nudge", "sms_link", "whatsapp_nudge"]),
    "call_first": _make_playbook("call_first", ["voice_call", "voice_call", "whatsapp_nudge"]),
    "standard_playbook": _make_playbook("standard_playbook",
                                        ["silent_retry", "sms_link", "whatsapp_nudge",
                                         "voice_call", "human_escalation"]),
}


def run_policy_on(name: str, accounts, seed: int, stress: float, voice_budget: float,
                  human_cap: int):
    """Run one named policy over an explicit list of accounts."""
    fn = POLICIES[name]
    guard = Guardrails(accounts, voice_budget=voice_budget, human_cap=human_cap)
    episodes, events = [], []
    for account in accounts:
        # per-account RNG shared across policies -> common random numbers,
        # so policy deltas are less about luck and more about decisions.
        rng = random.Random(f"{seed}:{account.account_id}")
        ep, evs = fn(account, guard, rng, stress, guard.now0.replace(hour=0))
        episodes.append(ep)
        events.extend(evs)
    return episodes, events


def run_policy(name: str, ledger, seed: int, stress: float, voice_budget: float,
               human_cap: int):
    """Run one named policy over the whole ledger. Returns (episodes, events)."""
    return run_policy_on(name, ledger, seed, stress, voice_budget, human_cap)
