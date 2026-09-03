"""guardrails.py -- a HARD GATE. Not a system prompt. Not a suggestion.

`Guardrails.check()` is called on every candidate action before it can be
played; a rejected action is removed from the choice set, so the agent
physically cannot emit it. That is the difference between a guardrail and a
line in a prompt: the prompt can be argued with.

`audit_executed()` is the second half. It re-derives every rule FROM SCRATCH
against the emitted JSONL trail, deliberately without importing the
`Guardrails` class, so a bug in the gate shows up as a counted violation in
the final scorecard instead of leaking silently. If the two implementations
ever disagree, the report stops saying "0".
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .core import INTERVENTION_COST, CONTACTING

# ---- the rules ---------------------------------------------------------
WINDOW_START = time(9, 0)          # 09:00 local, inclusive
WINDOW_END = time(19, 0)           # 19:00 local, exclusive
CONTACT_CAP_7D = 3                 # max contacting touches per rolling 7 days
VOICE_ATTEMPTS_MAX = 2             # max voice dials per account, ever
DEFAULT_VOICE_BUDGET = 1200.0      # rupees of voice spend across the whole run
DEFAULT_HUMAN_CAP = 25            # manual escalations a human team can absorb per run
NOW0 = datetime(2026, 9, 3, 0, 0)  # reference epoch for seeded history

_EPS = 1e-9

VIOLATION_CODES = (
    "dnc", "contact_window", "contact_cap_7d", "ptp_suppression",
    "no_phone", "voice_attempts_max", "voice_budget", "human_capacity",
)


class Guardrails:
    """Stateful gate. One instance per policy run (the voice budget is global)."""

    def __init__(self, ledger, voice_budget: float = DEFAULT_VOICE_BUDGET,
                 human_cap: int = DEFAULT_HUMAN_CAP, now0: datetime | None = None):
        self.voice_budget = float(voice_budget)
        self.voice_spent = 0.0
        self.human_cap = int(human_cap)
        self.human_used = 0
        self.now0 = now0 or NOW0
        self.history: dict[str, list[tuple[datetime, str]]] = {}
        self.ptp: dict[str, date] = {}
        for a in ledger:
            # Seed each account's recent-contact history so contacts_last_7d
            # actually constrains the first stage.
            self.history[a.account_id] = [
                (self.now0 - timedelta(days=1 + k), "sms_link")
                for k in range(a.contacts_last_7d)
            ]
            if a.promise_to_pay_due:
                self.ptp[a.account_id] = date.fromisoformat(a.promise_to_pay_due)

    # -- internal counters ------------------------------------------------
    def _contacts_7d(self, aid: str, now: datetime) -> int:
        cutoff = now - timedelta(days=7)
        return sum(1 for (t, iv) in self.history[aid]
                   if iv in CONTACTING and t > cutoff)

    def _voice_attempts(self, aid: str) -> int:
        return sum(1 for (_, iv) in self.history[aid] if iv == "voice_call")

    # -- the gate -------------------------------------------------------
    def check(self, account, intervention: str, now: datetime) -> tuple[bool, str | None]:
        """Return (allowed, violation_code). violation_code is None iff allowed."""
        aid = account.account_id
        cost = INTERVENTION_COST[intervention]
        contacting = intervention in CONTACTING

        if contacting and account.dnc:
            return False, "dnc"
        if contacting and not (WINDOW_START <= now.time() < WINDOW_END):
            return False, "contact_window"
        if contacting and self._contacts_7d(aid, now) >= CONTACT_CAP_7D:
            return False, "contact_cap_7d"
        if contacting and aid in self.ptp and now.date() < self.ptp[aid]:
            return False, "ptp_suppression"
        if intervention == "voice_call":
            if not account.has_phone:
                return False, "no_phone"
            if self._voice_attempts(aid) >= VOICE_ATTEMPTS_MAX:
                return False, "voice_attempts_max"
            if self.voice_spent + cost > self.voice_budget + _EPS:
                return False, "voice_budget"
        if intervention == "human_escalation" and self.human_used >= self.human_cap:
            return False, "human_capacity"
        return True, None

    # -- state mutation (only after an action is actually played) --------
    def commit(self, account, intervention: str, now: datetime, cost: float) -> None:
        self.history[account.account_id].append((now, intervention))
        if intervention == "voice_call":
            self.voice_spent += cost
        elif intervention == "human_escalation":
            self.human_used += 1

    def register_ptp(self, account_id: str, due: date) -> None:
        self.ptp[account_id] = due


# --------------------------------------------------------------------------
# Independent post-hoc auditor -- re-derives every rule from the JSONL trail.
# Intentionally does NOT touch the Guardrails class above.
# --------------------------------------------------------------------------

def audit_executed(events: list[dict], ledger, voice_budget: float = DEFAULT_VOICE_BUDGET,
                   human_cap: int = DEFAULT_HUMAN_CAP,
                   now0: datetime | None = None) -> list[dict]:
    """Return one record per rule violation found in `events`. Should be empty."""
    now0 = now0 or NOW0
    acc = {a.account_id: a for a in ledger}
    human_used = 0

    contacts: dict[str, list[datetime]] = {}
    voice_ct: dict[str, int] = {}
    ptp: dict[str, date] = {}
    for a in ledger:
        contacts[a.account_id] = [now0 - timedelta(days=1 + k)
                                  for k in range(a.contacts_last_7d)]
        voice_ct[a.account_id] = 0
        if a.promise_to_pay_due:
            ptp[a.account_id] = date.fromisoformat(a.promise_to_pay_due)

    voice_spent = 0.0
    violations: list[dict] = []

    for ev in events:
        et = ev.get("event")
        aid = ev.get("account_id")
        if et == "ptp_created":
            ptp[aid] = date.fromisoformat(ev["due"])
            continue
        if et != "action":
            continue

        a = acc[aid]
        iv = ev["intervention"]
        now = datetime.fromisoformat(ev["ts"])
        cost = float(ev["cost"])
        contacting = iv in CONTACTING
        hits: list[str] = []

        if contacting and a.dnc:
            hits.append("dnc")
        if contacting and not (WINDOW_START <= now.time() < WINDOW_END):
            hits.append("contact_window")
        if contacting:
            recent = [t for t in contacts[aid] if t > now - timedelta(days=7)]
            if len(recent) >= CONTACT_CAP_7D:
                hits.append("contact_cap_7d")
        if contacting and aid in ptp and now.date() < ptp[aid]:
            hits.append("ptp_suppression")
        if iv == "voice_call":
            if not a.has_phone:
                hits.append("no_phone")
            if voice_ct[aid] >= VOICE_ATTEMPTS_MAX:
                hits.append("voice_attempts_max")
            if voice_spent + cost > voice_budget + _EPS:
                hits.append("voice_budget")
        if iv == "human_escalation" and human_used >= human_cap:
            hits.append("human_capacity")

        for code in hits:
            violations.append({
                "rule": code,
                "account_id": aid,
                "intervention": iv,
                "ts": ev["ts"],
                "stage": ev.get("stage"),
            })

        if contacting:
            contacts[aid].append(now)
        if iv == "voice_call":
            voice_ct[aid] += 1
            voice_spent += cost
        elif iv == "human_escalation":
            human_used += 1

    return violations
