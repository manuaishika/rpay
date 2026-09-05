"""world.py -- STATED PRIORS. Every number below is an ASSUMPTION.

    +-----------------------------------------------------------------+
    |  NOTHING IN THIS FILE IS REAL DATA.                            |
    |                                                                 |
    |  Every probability here was invented by the author to be       |
    |  internally consistent and directionally defensible. None of   |
    |  it has been fit to a Razorpay ledger or any other ledger.     |
    |  Do not deploy on these numbers. Run with --stress to see how  |
    |  quickly the ranking of policies falls apart when the voice    |
    |  assumptions are wrong.                                         |
    +-----------------------------------------------------------------+

PRIORS[reason][intervention] = p(recover | fresh failure of `reason`, one
application of `intervention`). A missing (reason, intervention) pair means
0.0 -- e.g. silent_retry on a revoked mandate.

Voice entries are conversion GIVEN THE CALL CONNECTS. The 0.62 pickup rate
is applied separately (see voice_connect_conversion / ladder.py) so the
option value of a connected-but-unconverted call is modelled explicitly.
"""
from __future__ import annotations

ASSUMPTION_BANNER = (
    "=" * 70 + "\n"
    "  world.py PRIORS ARE ASSUMPTIONS, NOT DATA. Not fit to any ledger.\n"
    "  Voice lift is the fragile hinge -- always cross-check with --stress.\n"
    "  (voice-call COST is derived, not guessed -- see recovery/costs.py)\n"
    + "=" * 70
)

# ---- voice-specific assumptions ------------------------------------------
VOICE_PICKUP_RATE = 0.62          # ASSUMPTION: fraction of dials that connect
PTP_CAPTURE_ON_CONNECT = 0.35     # ASSUMPTION: connected, did not pay, but promised
PTP_CONVERSION = 0.70             # ASSUMPTION: a dated promise-to-pay actually pays
PROMISE_HORIZON_DAYS = 3          # ASSUMPTION: how far out promises are dated

# ---- p(recover | reason, intervention) ---------------------------------
# human_escalation is only a MODEST lift over a connected voice call for most
# reasons (the customer still has to fix the underlying thing). Its real niche
# is high-value disputes -- and it is capacity-capped in guardrails.py, so the
# agent cannot lean on it. Numbers below are unconditional (no pickup gate).
PRIORS: dict[str, dict[str, float]] = {
    "insufficient_funds": {
        # a call cannot conjure money -- immediate conversion is low; the value
        # of voice here is capturing a DATED promise-to-pay (see option value).
        "silent_retry": 0.15, "sms_link": 0.08, "whatsapp_nudge": 0.12,
        "voice_call": 0.12, "human_escalation": 0.14,
    },
    "bank_downtime": {
        "silent_retry": 0.48, "sms_link": 0.10, "whatsapp_nudge": 0.14,
        "voice_call": 0.14, "human_escalation": 0.14,
    },
    "technical_decline": {
        "silent_retry": 0.36, "sms_link": 0.12, "whatsapp_nudge": 0.16,
        "voice_call": 0.28, "human_escalation": 0.30,
    },
    "limit_exceeded": {
        "silent_retry": 0.18, "sms_link": 0.10, "whatsapp_nudge": 0.14,
        "voice_call": 0.30, "human_escalation": 0.26,
    },
    "do_not_honour": {
        "silent_retry": 0.09, "sms_link": 0.06, "whatsapp_nudge": 0.09,
        "voice_call": 0.14, "human_escalation": 0.28,
    },
    "mandate_expired": {
        # silent_retry against an expired mandate does nothing.
        "sms_link": 0.15, "whatsapp_nudge": 0.22,
        "voice_call": 0.45, "human_escalation": 0.40,
    },
    "mandate_revoked": {
        # Retrying a revoked mandate is 0.0 -- the authorisation is gone.
        "silent_retry": 0.0,
        "sms_link": 0.05, "whatsapp_nudge": 0.08,
        "voice_call": 0.20, "human_escalation": 0.22,
    },
    "card_expired": {
        "sms_link": 0.14, "whatsapp_nudge": 0.20,
        "voice_call": 0.42, "human_escalation": 0.38,
    },
    "invoice_overdue": {
        "silent_retry": 0.02, "sms_link": 0.10, "whatsapp_nudge": 0.16,
        "voice_call": 0.38, "human_escalation": 0.48,
    },
    "checkout_abandoned": {
        # never reached a payment attempt -- there is nothing to retry, and a
        # phone call about an unfinished cart reads as intrusive rather than
        # helpful. A reminder link is the natural channel here; consistent
        # with commonly-reported cart-recovery ranges (link nudges recovering
        # a low-teens to twenties percentage), not fit to any dataset.
        "sms_link": 0.16, "whatsapp_nudge": 0.20,
        "voice_call": 0.10, "human_escalation": 0.12,
    },
}


def p_recover(reason: str, intervention: str, stress: float = 1.0) -> float:
    """Stated prior for one intervention.

    `stress` is a sensitivity knob that scales the VOICE lift only (leaving
    every other channel untouched), so a single run can answer "how wrong
    do the voice assumptions have to be before calling stops paying?".
    """
    base = PRIORS.get(reason, {}).get(intervention, 0.0)
    if intervention == "voice_call" and stress != 1.0:
        base *= stress
    return max(0.0, min(1.0, base))


def voice_connect_conversion(reason: str, stress: float = 1.0) -> float:
    """Unconditional p(recover) from a single voice attempt = pickup * convert."""
    return VOICE_PICKUP_RATE * p_recover(reason, "voice_call", stress)
