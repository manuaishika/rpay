"""guardrails.py -- a HARD GATE. Not a system prompt. Not a suggestion.

`Guardrails.check()` is called on every candidate action before it can be
played; a rejected action is removed from the choice set, so the agent
physically cannot emit it. That is the difference between a guardrail and a
line in a prompt: the prompt can be argued with.

The contact rules live in `ContactPolicy` -- a merchant-tunable config with
CONFIGURABLE DEFAULTS, not a legal assertion. The window, frequency cap and
voice-attempt ceiling were chosen by the author, not lifted from any RBI or
TRAI circular; mapping them to real regulation is pre-production work.

`audit_executed()` is the second half. It re-derives every rule FROM SCRATCH
against the emitted JSONL trail, deliberately without importing the
`Guardrails` class, so a bug in the gate shows up as a counted violation in
the final scorecard instead of leaking silently. If the two implementations
ever disagree, the report stops saying "0".
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .core import INTERVENTION_COST, CONTACTING


@dataclass(frozen=True)
class ContactPolicy:
    """A merchant-tunable outreach policy.

    These values are CONFIGURABLE DEFAULTS chosen to be conservative. They are
    NOT taken from any RBI or TRAI circular -- the 09:00-19:00 window, the
    3-contacts-per-7-days cap and the 2-voice-attempts ceiling were picked by
    the author, not sourced. Mapping them to the actual RBI recovery-agent
    guidelines and TRAI UCC/DND regulation is required before production use.
    Pass a different `ContactPolicy` to `Guardrails(...)` / `audit_executed(...)`
    to run under a different merchant's rules.
    """
    window_start: time = time(9, 0)      # local, inclusive        -- ESTIMATED
    window_end: time = time(19, 0)       # local, exclusive        -- ESTIMATED
    contacts_per_7d: int = 3             # contacting touches / 7d  -- ESTIMATED
    voice_attempts_max: int = 2          # voice dials per account  -- ESTIMATED


CONTACT_POLICY = ContactPolicy()

# Back-compat module aliases -- everything reads these; they are just the
# default policy's fields exposed by their old names.
WINDOW_START = CONTACT_POLICY.window_start
WINDOW_END = CONTACT_POLICY.window_end
CONTACT_CAP_7D = CONTACT_POLICY.contacts_per_7d
VOICE_ATTEMPTS_MAX = CONTACT_POLICY.voice_attempts_max

DEFAULT_VOICE_BUDGET = 1200.0      # rupees of voice spend across the whole run -- ESTIMATED
DEFAULT_HUMAN_CAP = 25            # manual escalations a human team can absorb per run -- ESTIMATED
NOW0 = datetime(2026, 9, 3, 0, 0)  # reference epoch for seeded history

_EPS = 1e-9

VIOLATION_CODES = (
    "dnc", "contact_window", "contact_cap_7d", "ptp_suppression",
    "no_phone", "voice_attempts_max", "voice_budget", "human_capacity",
)


class Guardrails:
    """Stateful gate. One instance per policy run (the voice budget is global)."""

    def __init__(self, ledger, voice_budget: float = DEFAULT_VOICE_BUDGET,
                 human_cap: int = DEFAULT_HUMAN_CAP, now0: datetime | None = None,
                 policy: ContactPolicy = CONTACT_POLICY):
        self.policy = policy
        self.voice_budget = float(voice_budget)
        self.voice_spent = 0.0
        self.human_cap = int(human_cap)
        self.human_used = 0
        self.lock = threading.RLock()   # the console runs episodes concurrently
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
        with self.lock:
            return self._check(account, intervention, now)

    def _check(self, account, intervention: str, now: datetime) -> tuple[bool, str | None]:
        aid = account.account_id
        cost = INTERVENTION_COST[intervention]
        contacting = intervention in CONTACTING
        pol = self.policy

        if contacting and account.dnc:
            return False, "dnc"
        if contacting and not (pol.window_start <= now.time() < pol.window_end):
            return False, "contact_window"
        if contacting and self._contacts_7d(aid, now) >= pol.contacts_per_7d:
            return False, "contact_cap_7d"
        if contacting and aid in self.ptp and now.date() < self.ptp[aid]:
            return False, "ptp_suppression"
        if intervention == "voice_call":
            if not account.has_phone:
                return False, "no_phone"
            if self._voice_attempts(aid) >= pol.voice_attempts_max:
                return False, "voice_attempts_max"
            if self.voice_spent + cost > self.voice_budget + _EPS:
                return False, "voice_budget"
        if intervention == "human_escalation" and self.human_used >= self.human_cap:
            return False, "human_capacity"
        return True, None

    # -- state mutation (only after an action is actually played) --------
    def commit(self, account, intervention: str, now: datetime, cost: float) -> None:
        with self.lock:
            self.history[account.account_id].append((now, intervention))
            if intervention == "voice_call":
                self.voice_spent += cost
            elif intervention == "human_escalation":
                self.human_used += 1

    def commit_if_allowed(self, account, intervention: str, now: datetime, cost: float):
        """Atomic re-check + commit, for the concurrent console. Returns (ok, code)."""
        with self.lock:
            ok, code = self._check(account, intervention, now)
            if ok:
                self.history[account.account_id].append((now, intervention))
                if intervention == "voice_call":
                    self.voice_spent += cost
                elif intervention == "human_escalation":
                    self.human_used += 1
            return ok, code

    def register_ptp(self, account_id: str, due: date) -> None:
        with self.lock:
            self.ptp[account_id] = due


# --------------------------------------------------------------------------
# Independent post-hoc auditor -- re-derives every rule from the JSONL trail.
# Intentionally does NOT touch the Guardrails class above.
# --------------------------------------------------------------------------

def audit_executed(events: list[dict], ledger, voice_budget: float = DEFAULT_VOICE_BUDGET,
                   human_cap: int = DEFAULT_HUMAN_CAP,
                   now0: datetime | None = None,
                   policy: ContactPolicy = CONTACT_POLICY) -> list[dict]:
    """Return one record per rule violation found in `events`. Should be empty."""
    now0 = now0 or NOW0
    pol = policy
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
        if contacting and not (pol.window_start <= now.time() < pol.window_end):
            hits.append("contact_window")
        if contacting:
            recent = [t for t in contacts[aid] if t > now - timedelta(days=7)]
            if len(recent) >= pol.contacts_per_7d:
                hits.append("contact_cap_7d")
        if contacting and aid in ptp and now.date() < ptp[aid]:
            hits.append("ptp_suppression")
        if iv == "voice_call":
            if not a.has_phone:
                hits.append("no_phone")
            if voice_ct[aid] >= pol.voice_attempts_max:
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
