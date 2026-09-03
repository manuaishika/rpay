"""Stdlib self-checks:  python tests.py   (or python -m unittest)."""
import unittest
from datetime import datetime, timedelta

from recovery import core, world
from recovery.guardrails import (
    Guardrails, audit_executed, CONTACT_CAP_7D, VOICE_ATTEMPTS_MAX, NOW0,
)
from recovery.ladder import POLICIES, run_policy


def _acc(**kw):
    base = dict(account_id="acc_0001", segment="B2C", reason="insufficient_funds",
                amount=5000.0, tenure_months=10, prior_failures=0, language="hinglish",
                dnc=False, contacts_last_7d=0, has_phone=True, promise_to_pay_due=None)
    base.update(kw)
    return core.Account(**base)


class Taxonomy(unittest.TestCase):
    def test_partition(self):
        self.assertEqual(core.TRANSIENT | core.ACTION_REQUIRED,
                         frozenset(core.FAILURE_REASONS))
        self.assertFalse(core.TRANSIENT & core.ACTION_REQUIRED)

    def test_revoked_mandate_retry_is_zero(self):
        self.assertEqual(world.p_recover("mandate_revoked", "silent_retry"), 0.0)

    def test_stress_scales_voice_only(self):
        self.assertAlmostEqual(world.p_recover("card_expired", "voice_call", 0.5),
                               world.p_recover("card_expired", "voice_call") * 0.5)
        self.assertEqual(world.p_recover("card_expired", "sms_link", 0.5),
                         world.p_recover("card_expired", "sms_link"))


class Gate(unittest.TestCase):
    def setUp(self):
        self.mid = datetime(2026, 9, 3, 12, 0)

    def _g(self, acc, **kw):
        return Guardrails([acc], **kw)

    def test_dnc_blocks_contact_not_retry(self):
        a = _acc(dnc=True)
        g = self._g(a)
        self.assertFalse(g.check(a, "voice_call", self.mid)[0])
        self.assertEqual(g.check(a, "sms_link", self.mid)[1], "dnc")
        self.assertTrue(g.check(a, "silent_retry", self.mid)[0])

    def test_contact_window(self):
        a = _acc()
        g = self._g(a)
        self.assertFalse(g.check(a, "sms_link", datetime(2026, 9, 3, 6, 0))[0])
        self.assertFalse(g.check(a, "sms_link", datetime(2026, 9, 3, 19, 0))[0])
        self.assertTrue(g.check(a, "sms_link", datetime(2026, 9, 3, 18, 59))[0])

    def test_contact_cap(self):
        a = _acc(contacts_last_7d=CONTACT_CAP_7D)
        g = self._g(a)
        self.assertEqual(g.check(a, "sms_link", self.mid)[1], "contact_cap_7d")

    def test_voice_attempts_max(self):
        a = _acc()
        g = self._g(a)
        for _ in range(VOICE_ATTEMPTS_MAX):
            g.commit(a, "voice_call", self.mid, 12.0)
        self.assertEqual(g.check(a, "voice_call", self.mid)[1], "voice_attempts_max")

    def test_voice_budget(self):
        a = _acc()
        g = self._g(a, voice_budget=12.0)
        g.commit(a, "voice_call", self.mid, 12.0)
        self.assertEqual(g.check(a, "voice_call", self.mid)[1], "voice_budget")

    def test_ptp_suppression(self):
        due = (self.mid + timedelta(days=2)).date().isoformat()
        a = _acc(promise_to_pay_due=due)
        g = self._g(a)
        self.assertEqual(g.check(a, "whatsapp_nudge", self.mid)[1], "ptp_suppression")

    def test_human_capacity(self):
        a = _acc()
        g = self._g(a, human_cap=0)
        self.assertEqual(g.check(a, "human_escalation", self.mid)[1], "human_capacity")


class EndToEnd(unittest.TestCase):
    def test_zero_violations_every_policy(self):
        ledger = core.build_ledger(20260903)
        for name in POLICIES:
            _, events = run_policy(name, ledger, 20260903, 1.0, 1200.0, 25)
            v = audit_executed(events, ledger, 1200.0, 25)
            self.assertEqual(v, [], f"{name} leaked: {v[:3]}")

    def test_determinism(self):
        ledger = core.build_ledger(7)
        a = run_policy("ladder", ledger, 7, 1.0, 1200.0, 25)[0]
        b = run_policy("ladder", ledger, 7, 1.0, 1200.0, 25)[0]
        self.assertEqual([e.recovered_amount for e in a],
                         [e.recovered_amount for e in b])

    def test_auditor_catches_a_planted_violation(self):
        # The auditor re-derives rules independently, so a gate that "forgot"
        # DNC would show up here as a counted violation rather than leaking.
        ledger = core.build_ledger(20260903)
        dnc_acc = next(a for a in ledger if a.dnc)
        planted = [{
            "event": "action", "account_id": dnc_acc.account_id,
            "ts": "2026-09-03T12:00:00", "intervention": "voice_call",
            "cost": 12.0, "stage": 1,
        }]
        v = audit_executed(planted, ledger, 1200.0, 25)
        self.assertTrue(any(x["rule"] == "dnc" for x in v))

    def test_ledger_is_heavy_tailed(self):
        led = core.build_ledger(20260903)
        amounts = sorted(a.amount for a in led)
        # top 5% of accounts hold a large share of the rupees
        top = sum(amounts[-13:]) / sum(amounts)
        self.assertGreater(top, 0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
