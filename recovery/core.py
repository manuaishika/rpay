"""core.py -- failure taxonomy, intervention costs, synthetic ledger.

Everything here is either a DEFINITION (taxonomy, rupee costs) or SYNTHETIC
data generated from a fixed seed. No real Razorpay data is used anywhere in
this package. Recovery probabilities live in world.py and are also invented.
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------

FAILURE_REASONS = (
    "insufficient_funds",
    "bank_downtime",
    "technical_decline",
    "mandate_expired",
    "mandate_revoked",
    "card_expired",
    "limit_exceeded",
    "do_not_honour",
    "invoice_overdue",
    "checkout_abandoned",
)

# TRANSIENT      -- the money may simply appear if we wait / retry.
# ACTION_REQUIRED -- nothing changes until the customer does something.
TRANSIENT = frozenset({
    "insufficient_funds",
    "bank_downtime",
    "technical_decline",
    "limit_exceeded",
    "do_not_honour",
})
ACTION_REQUIRED = frozenset({
    "mandate_expired",
    "mandate_revoked",
    "card_expired",
    "invoice_overdue",
    "checkout_abandoned",
})

assert TRANSIENT | ACTION_REQUIRED == frozenset(FAILURE_REASONS)
assert not (TRANSIENT & ACTION_REQUIRED)

# Where in the revenue funnel the money is at risk -- mirrors "payment
# failures and checkout abandonment to overdue receivables" (Track 03 brief)
# rather than treating every reason as the same kind of loss.
RISK_STAGE = {
    "checkout_abandoned": "checkout",
    "invoice_overdue": "receivable",
}


def risk_stage(reason: str) -> str:
    return RISK_STAGE.get(reason, "payment")

# --------------------------------------------------------------------------
# Interventions and their marginal cost in rupees
# --------------------------------------------------------------------------

INTERVENTION_COST = {
    "silent_retry": 0.50,
    "sms_link": 0.20,
    "whatsapp_nudge": 0.35,
    "voice_call": 12.00,
    "human_escalation": 85.00,
}

# A "contacting" intervention touches the customer and is subject to the
# contact-window / DNC / frequency guardrails. silent_retry does not.
CONTACTING = frozenset({"sms_link", "whatsapp_nudge", "voice_call", "human_escalation"})

DEFAULT_VOICE_COST = INTERVENTION_COST["voice_call"]


@contextmanager
def voice_cost(rupees: float):
    """Temporarily override the voice-call cost (for sensitivity sweeps).

    The whole thesis pivots on this number, so the sweep needs to move it.
    Single-threaded, sequential use only -- it mutates the module dict and
    restores it on exit.
    """
    old = INTERVENTION_COST["voice_call"]
    INTERVENTION_COST["voice_call"] = float(rupees)
    try:
        yield
    finally:
        INTERVENTION_COST["voice_call"] = old

# --------------------------------------------------------------------------
# Synthetic ledger
# --------------------------------------------------------------------------

LANGUAGES = ("hinglish", "hindi", "english", "tamil", "telugu", "marathi", "bengali")
_LANG_WEIGHTS = (0.34, 0.25, 0.15, 0.08, 0.07, 0.06, 0.05)

_REASON_WEIGHTS = {
    "insufficient_funds": 0.22,
    "checkout_abandoned": 0.20,   # never reached a payment attempt at all
    "invoice_overdue": 0.15,
    "technical_decline": 0.10,
    "bank_downtime": 0.08,
    "do_not_honour": 0.08,
    "card_expired": 0.07,
    "limit_exceeded": 0.05,
    "mandate_expired": 0.03,
    "mandate_revoked": 0.02,
}

LEDGER_EPOCH = datetime(2026, 9, 3)


@dataclass
class Account:
    account_id: str
    segment: str                 # "B2C" or "B2B"
    reason: str                  # a FAILURE_REASONS member
    amount: float                # rupees at stake on this failed payment
    tenure_months: int
    prior_failures: int          # lifetime count of prior payment failures
    language: str
    dnc: bool                    # permanent do-not-contact
    contacts_last_7d: int        # outreach already spent on this account
    has_phone: bool
    promise_to_pay_due: str | None = None   # ISO date if a PTP already exists

    def as_dict(self) -> dict:
        return asdict(self)


def build_ledger(seed: int = 20260903, n: int = 250) -> list[Account]:
    """Deterministic synthetic ledger of `n` failed-payment accounts."""
    rng = random.Random(seed)
    reasons = list(_REASON_WEIGHTS)
    weights = list(_REASON_WEIGHTS.values())
    accounts: list[Account] = []

    for i in range(n):
        is_b2b = rng.random() < 0.22
        if is_b2b:
            # heavy-tailed: high-sigma lognormal, median ~ e^10.1 ~ Rs.24k,
            # with a long right tail into the lakhs.
            amount = rng.lognormvariate(10.1, 1.35)
        else:
            amount = rng.lognormvariate(7.1, 0.70)   # median ~ Rs.1.2k
        amount = round(min(amount, 5_000_000.0), 2)

        reason = rng.choices(reasons, weights)[0]
        tenure = int(rng.expovariate(1 / 14)) + 1
        prior = rng.choices([0, 1, 2, 3, 4, 5],
                            [0.40, 0.25, 0.15, 0.10, 0.06, 0.04])[0]
        language = rng.choices(LANGUAGES, _LANG_WEIGHTS)[0]
        dnc = rng.random() < 0.08
        contacts_7d = rng.choices([0, 1, 2, 3], [0.72, 0.18, 0.07, 0.03])[0]
        has_phone = rng.random() < 0.94

        ptp = None
        if rng.random() < 0.05:
            ptp = (LEDGER_EPOCH + timedelta(days=rng.randint(1, 6))).date().isoformat()

        accounts.append(Account(
            account_id=f"acc_{i:04d}",
            segment="B2B" if is_b2b else "B2C",
            reason=reason,
            amount=amount,
            tenure_months=tenure,
            prior_failures=prior,
            language=language,
            dnc=dnc,
            contacts_last_7d=contacts_7d,
            has_phone=has_phone,
            promise_to_pay_due=ptp,
        ))
    return accounts


def ledger_summary(accounts: list[Account]) -> dict:
    total = sum(a.amount for a in accounts)
    b2b = [a for a in accounts if a.segment == "B2B"]
    stages = ("checkout", "payment", "receivable")
    return {
        "accounts": len(accounts),
        "at_risk_rupees": round(total, 2),
        "b2b_accounts": len(b2b),
        "b2b_share_of_rupees": round(sum(a.amount for a in b2b) / total, 3) if total else 0.0,
        "dnc_accounts": sum(1 for a in accounts if a.dnc),
        "with_open_ptp": sum(1 for a in accounts if a.promise_to_pay_due),
        "by_reason": {r: sum(1 for a in accounts if a.reason == r) for r in FAILURE_REASONS},
        "by_stage": {s: sum(1 for a in accounts if risk_stage(a.reason) == s) for s in stages},
    }
