"""agent.py -- an LLM in the decision seat.

Same environment, same hard guardrails, same 5-stage bound as ladder.py.
The difference: instead of argmax over expected net, sarvam-105b picks the
next action and says why -- given the account, what already failed this
episode, the menu of actions the guardrail gate PERMITS right now, their
costs, the believed recovery odds, and the voice budget left.

The gate still filters the menu, so the model cannot choose a blocked
action; audit_executed() still returns 0. If there is no API key or the
call fails, the agent falls back to the expected-value rule so the console
always runs -- every decision is tagged with its source ("llm" / "rule").
"""
from __future__ import annotations

import json
import random

from . import core, world
from .guardrails import Guardrails
from datetime import timedelta

from .ladder import (
    MAX_STAGES, Episode, _advance, _channel_p, _ev, _expected_net,
    _resolve_ptp, _simulate, _start,
)

AGENT_SYSTEM = (
    "You are a revenue-recovery agent for a business that collects payments through "
    "Razorpay. A customer payment has failed. Your job each turn: choose the SINGLE "
    "next action that maximises expected NET recovery (rupees recovered minus rupees "
    "spent) over the life of this case, while protecting customer goodwill and the "
    "shared voice budget. You may also choose \"stop\" when no action is worth its cost.\n\n"
    "You will be given ONLY the actions currently permitted by compliance guardrails. "
    "Choose exactly one of them, or \"stop\". The naive expected-net figure is provided "
    "as a hint; override it when the account context justifies it -- a long-tenure "
    "high-value B2B account with a fixable problem can deserve a call the naive math "
    "would skip, and a tiny balance with many prior failures rarely does.\n\n"
    "Respond with STRICT JSON and nothing else: "
    '{"action": "<menu key or stop>", "rationale": "<= 160 chars, plain English", '
    '"confidence": <0..1>}'
)

_MODEL = "sarvam-105b-conversations"


def _menu(account, ep: Episode, guard: Guardrails, now, stress: float):
    """Split the action set into (permitted menu with economics, blocked rows)."""
    menu, blocked = [], []
    for iv in core.INTERVENTION_COST:
        ok, code = guard.check(account, iv, now)
        if not ok:
            blocked.append({"intervention": iv, "rule": code})
            continue
        net, _ = _expected_net(account.reason, iv, account, ep, stress)
        menu.append({
            "action": iv,
            "cost_rupees": core.INTERVENTION_COST[iv],
            "believed_recovery_p": round(_channel_p(account.reason, iv, account, ep, stress), 3),
            "naive_expected_net_rupees": round(net, 2),
        })
    return menu, blocked


def _situation(account, ep: Episode, menu, budget_left, stage):
    tried = [f"{k}x{v}" for k, v in ep.attempts_by_channel.items()]
    return {
        "stage": stage,
        "max_stages": MAX_STAGES,
        "account": {
            "segment": account.segment,
            "amount_at_stake_rupees": round(account.amount, 2),
            "failure_reason": account.reason,
            "reason_class": "transient" if account.reason in core.TRANSIENT else "action_required",
            "tenure_months": account.tenure_months,
            "prior_failures_lifetime": account.prior_failures,
            "language": account.language,
        },
        "this_case_so_far": {
            "attempts": ep.total_attempts,
            "channels_used": tried or ["none"],
            "spent_rupees": round(ep.spend, 2),
        },
        "voice_budget_left_rupees": round(budget_left, 2),
        "permitted_actions": menu,
        "note": "believed_recovery_p and naive_expected_net are model assumptions, not measured data.",
    }


def _rule_choice(account, ep, menu, stress):
    """Expected-value fallback: highest naive net, stop if nothing clears zero."""
    if not menu:
        return "stop", "no compliant channel", "rule"
    best = max(menu, key=lambda m: m["naive_expected_net_rupees"])
    if best["naive_expected_net_rupees"] <= 0:
        return "stop", "no action clears its cost in expectation", "rule"
    return best["action"], (
        f"best naive net Rs.{best['naive_expected_net_rupees']:.0f} via {best['action']}"), "rule"


def decide(client, account, ep, menu, budget_left, stage, stress):
    """Return (action, rationale, source). source in {'llm','rule','rule_fallback'}."""
    if not menu:
        return "stop", "no compliant channel left", "rule"
    if client is None:
        return _rule_choice(account, ep, menu, stress)

    keys = {m["action"] for m in menu} | {"stop"}
    messages = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": json.dumps(_situation(account, ep, menu, budget_left, stage))},
    ]
    for attempt in range(2):
        try:
            raw = client.chat(messages, model=_MODEL, temperature=0.2, max_tokens=450,
                              reasoning_effort="low")
        except Exception as e:                                  # noqa: BLE001
            act, why, _ = _rule_choice(account, ep, menu, stress)
            return act, f"{why} (llm error: {str(e)[:60]})", "rule_fallback"
        try:
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            action = str(data["action"]).strip()
            rationale = str(data.get("rationale", ""))[:180]
        except (ValueError, KeyError):
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Invalid. Reply with only the JSON object."})
            continue
        if action in keys:
            return action, rationale or "(no rationale)", "llm"
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user",
                         "content": f"'{action}' is not permitted. Choose from {sorted(keys)}."})
    act, why, _ = _rule_choice(account, ep, menu, stress)
    return act, f"{why} (llm did not return a valid action)", "rule_fallback"


def run_agent_episode(account, guard: Guardrails, rng: random.Random, stress: float,
                      client, now0, emit=None):
    now = _start(account, now0)
    ep = Episode(account_id=account.account_id, policy="agent")
    events = []

    def push(ev):
        events.append(ev)
        if emit:
            emit(ev)

    push(_ev(account, "episode_start", now, policy="agent"))

    for stage in range(1, MAX_STAGES + 1):
        menu, blocked = _menu(account, ep, guard, now, stress)
        for b in blocked:
            push(_ev(account, "blocked", now, stage=stage, **b))
        budget_left = guard.voice_budget - guard.voice_spent

        action, rationale, source = decide(client, account, ep, menu, budget_left, stage, stress)

        if action == "stop":
            ep.stopped_reason = "agent_stop" if source == "llm" else "rule_stop"
            push(_ev(account, "stop", now, stage=stage, cause=ep.stopped_reason,
                     rationale=rationale, decided_by=source))
            break

        cost = core.INTERVENTION_COST[action]
        # atomic re-check: another concurrent episode may have just spent the
        # last of the voice budget between menu-build and here.
        ok, code = guard.commit_if_allowed(account, action, now, cost)
        if not ok:
            push(_ev(account, "blocked", now, stage=stage, intervention=action, rule=code,
                     rationale=f"chosen action no longer permitted ({code})", decided_by=source))
            now = _advance(now)
            continue

        net = next((m["naive_expected_net_rupees"] for m in menu if m["action"] == action), None)
        outcome = _simulate(rng, account.reason, action, account, ep, stress)
        ep.spend += cost
        ep.total_attempts += 1
        ep.attempts_by_channel[action] = ep.attempts_by_channel.get(action, 0) + 1
        push(_ev(account, "action", now, stage=stage, intervention=action, cost=cost,
                 outcome=outcome, rationale=rationale, decided_by=source,
                 naive_expected_net=net))
        if outcome == "recovered":
            ep.recovered = True
            ep.recovered_amount = account.amount
            ep.recovered_via = action
        elif outcome == "ptp":
            due = (now + timedelta(days=world.PROMISE_HORIZON_DAYS)).date()
            guard.register_ptp(account.account_id, due)
            ep.ptp_due = due.isoformat()
            push(_ev(account, "ptp_created", now, stage=stage, due=ep.ptp_due))
        if outcome in ("recovered", "ptp"):
            break
        now = _advance(now)
    else:
        ep.stopped_reason = "max_stages"

    mark = len(events)
    _resolve_ptp(account, ep, events, rng, now)
    if emit:
        for ev in events[mark:]:
            emit(ev)
    end = _ev(account, "episode_end", now, recovered=ep.recovered,
              recovered_via=ep.recovered_via,
              recovered_amount=round(ep.recovered_amount, 2),
              spend=round(ep.spend, 2), stopped_reason=ep.stopped_reason)
    push(end)
    return ep, events
