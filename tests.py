"""Stdlib self-checks:  python tests.py   (or python -m unittest)."""
import random
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

    def test_checkout_abandoned_has_no_retry_path(self):
        # nothing was ever attempted, so there is nothing to retry
        self.assertEqual(world.p_recover("checkout_abandoned", "silent_retry"), 0.0)

    def test_risk_stage_mirrors_the_track_brief(self):
        self.assertEqual(core.risk_stage("checkout_abandoned"), "checkout")
        self.assertEqual(core.risk_stage("invoice_overdue"), "receivable")
        self.assertEqual(core.risk_stage("insufficient_funds"), "payment")

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


class DerivedCost(unittest.TestCase):
    def test_voice_cost_is_derived_and_sane(self):
        from recovery import costs
        c = costs.voice_call_cost()
        self.assertGreater(c, 3.0)
        self.assertLess(c, 12.0)
        self.assertEqual(core.INTERVENTION_COST["voice_call"], costs.VOICE_CALL)

    def test_breakdown_components_sum_to_total(self):
        from recovery import costs
        b = costs.breakdown()
        parts = (b["stt"] + b["tts"] + b["llm"]
                 + b["telephony_connected"] + b["failed_dial_amortised"])
        self.assertAlmostEqual(parts, b["per_connected_call"], places=1)

    def test_no_hardcoded_twelve(self):
        # the whole point of costs.py -- voice cost must not be a literal 12
        self.assertNotEqual(core.INTERVENTION_COST["voice_call"], 12.0)


class Assumptions(unittest.TestCase):
    def test_header_matches_counts(self):
        from recovery import assumptions
        c = assumptions.counts()
        h = assumptions.header()
        self.assertIn(f"{c['sourced']} sourced", h)
        self.assertIn(f"{c['estimated']} estimated", h)
        self.assertGreaterEqual(c["sourced"], 4)          # the 4 Sarvam rates
        self.assertGreater(c["estimated"], c["sourced"])

    def test_md_renders(self):
        from recovery import assumptions
        md = assumptions.render_md()
        self.assertIn("| **SOURCED** |", md)
        self.assertIn("| **ESTIMATED** |", md)
        self.assertIn("docs.sarvam.ai", md)


class ContactPolicyConfig(unittest.TestCase):
    def test_custom_policy_changes_the_gate(self):
        from recovery.guardrails import ContactPolicy
        a = _acc()
        strict = ContactPolicy(contacts_per_7d=0)
        g = Guardrails([a], policy=strict)
        ok, code = g.check(a, "sms_link", datetime(2026, 9, 3, 12, 0))
        self.assertFalse(ok)
        self.assertEqual(code, "contact_cap_7d")
        # default policy still allows it
        self.assertTrue(Guardrails([a]).check(a, "sms_link", datetime(2026, 9, 3, 12, 0))[0])


class Analysis(unittest.TestCase):
    def test_run_all_shape_and_cohort_conservation(self):
        from recovery.analysis import run_all
        res = run_all(20260903, 1.0, 1200.0, 25)
        self.assertEqual(len(res["policies"]), 5)
        self.assertEqual(res["total_violations"], 0)
        for p in res["policies"]:
            seg = p["by_segment"]
            self.assertEqual(seg["B2C"]["accounts"] + seg["B2B"]["accounts"],
                             p["accounts"])
            self.assertEqual(sum(r["accounts"] for r in p["by_reason"].values()),
                             p["accounts"])
            self.assertAlmostEqual(
                round(seg["B2C"]["recovered"] + seg["B2B"]["recovered"], 2),
                p["recovered"], places=2)

    def test_sigma_decouples_estimate_from_truth(self):
        from recovery.analysis import run_all
        from recovery import ladder
        base = run_all(20260903, 1.0, 1200.0, 25, sigma=0.0)
        noised = run_all(20260903, 1.0, 1200.0, 25, sigma=0.5)
        b = {p["policy"]: p for p in base["policies"]}
        n = {p["policy"]: p for p in noised["policies"]}
        # fixed playbooks don't score -> unaffected by sigma
        self.assertEqual(b["retry_only"]["net"], n["retry_only"]["net"])
        self.assertEqual(b["standard_playbook"]["net"], n["standard_playbook"]["net"])
        # the ladder's decisions do change, and the gate still holds
        self.assertNotEqual(b["ladder"]["net"], n["ladder"]["net"])
        self.assertEqual(noised["total_violations"], 0)
        # the context manager restores cleanly
        self.assertEqual(ladder._EST_SIGMA, 0.0)

    def test_estimate_factor_is_mean_preserving(self):
        from recovery import ladder
        with ladder.estimate_noise(0.5, "t"):
            fs = [ladder._estimate_factor(_acc(account_id=f"a{i}"), "voice_call")
                  for i in range(4000)]
        self.assertAlmostEqual(sum(fs) / len(fs), 1.0, delta=0.05)

    def test_voice_cost_override_restores(self):
        from recovery import core
        base = core.INTERVENTION_COST["voice_call"]
        with core.voice_cost(99.0):
            self.assertEqual(core.INTERVENTION_COST["voice_call"], 99.0)
        self.assertEqual(core.INTERVENTION_COST["voice_call"], base)

    def test_sweep_small_grid(self):
        from recovery.sweep import run_sweep
        s = run_sweep(7, stress_grid=[0.6, 1.2], cost_grid=[8.0, 20.0], include_misspec=False)
        self.assertEqual(len(s["cells"]), 4)
        self.assertEqual(sum(c["violations"] for c in s["cells"]), 0)
        self.assertIn("min_stress_ladder_wins_at_cost_12", s["crossover"])
        # cost override must not bleed out of the sweep
        from recovery import core
        self.assertEqual(core.INTERVENTION_COST["voice_call"], core.DEFAULT_VOICE_COST)


class AgentAndConsole(unittest.TestCase):
    def _sample(self):
        from recovery.serve import pick_sample
        led = core.build_ledger(20260903)
        return led, pick_sample(led, 10, 20260903)

    def test_sample_is_deterministic_and_spread(self):
        led, s1 = self._sample()
        _, s2 = self._sample()
        self.assertEqual([a.account_id for a in s1], [a.account_id for a in s2])
        self.assertEqual(len({a.account_id for a in s1}), len(s1))
        self.assertTrue(any(a.segment == "B2B" for a in s1))

    def test_agent_llm_choice_is_honoured_when_permitted(self):
        from recovery import agent
        from recovery.guardrails import Guardrails

        class FakeClient:
            def __init__(self, action): self.action = action
            def chat(self, *a, **k):
                return '{"action": "%s", "rationale": "test", "confidence": 0.9}' % self.action

        led = core.build_ledger(20260903)
        acc = next(a for a in led if a.reason == "insufficient_funds" and not a.dnc
                   and a.has_phone and a.contacts_last_7d == 0)
        guard = Guardrails([acc], voice_budget=1200.0)
        ep, events = agent.run_agent_episode(
            acc, guard, random.Random("x"), 1.0, FakeClient("sms_link"),
            guard.now0.replace(hour=0))
        acts = [e for e in events if e["event"] == "action"]
        self.assertTrue(acts)
        self.assertEqual(acts[0]["intervention"], "sms_link")
        self.assertEqual(acts[0]["decided_by"], "llm")

    def test_agent_falls_back_to_rule_on_bad_json(self):
        from recovery import agent
        from recovery.guardrails import Guardrails

        class BrokenClient:
            def chat(self, *a, **k): return "sorry no json here"

        led = core.build_ledger(20260903)
        acc = next(a for a in led if a.reason == "mandate_revoked")
        guard = Guardrails([acc], voice_budget=1200.0)
        ep, events = agent.run_agent_episode(
            acc, guard, random.Random("x"), 1.0, BrokenClient(),
            guard.now0.replace(hour=0))
        decided = [e.get("decided_by") for e in events
                   if e["event"] in ("action", "stop")]
        self.assertTrue(all(d in ("rule", "rule_fallback") for d in decided))

    def test_agent_cannot_pick_a_blocked_action(self):
        from recovery import agent
        from recovery.guardrails import Guardrails, audit_executed

        class DncPusher:            # always tries to contact a DNC account
            def chat(self, *a, **k):
                return '{"action": "voice_call", "rationale": "x", "confidence": 1}'

        led = core.build_ledger(20260903)
        acc = next(a for a in led if a.dnc and a.has_phone)
        guard = Guardrails([acc], voice_budget=1200.0)
        ep, events = agent.run_agent_episode(
            acc, guard, random.Random("x"), 1.0, DncPusher(),
            guard.now0.replace(hour=0))
        self.assertEqual(audit_executed(events, [acc], 1200.0), [])


class Voice(unittest.TestCase):
    """No network -- just the offline plumbing of the optional voice layer."""

    def test_build_messages_is_reason_specific(self):
        from recovery import voice
        m = voice.build_messages(_acc(reason="mandate_revoked", amount=9000.0))
        self.assertEqual(m[0]["role"], "system")
        self.assertIn("mandate_revoked", m[1]["content"])
        self.assertIn("Rs.9,000", m[1]["content"])

    def test_fallback_script_mentions_amount_and_fix(self):
        from recovery import voice
        s = voice._fallback_script(_acc(reason="card_expired", amount=1234.0))
        self.assertIn("1,234", s)
        self.assertIn("card", s.lower())
        self.assertLess(len(s), 800)

    def test_decided_calls_are_a_subset_that_got_voice(self):
        from recovery import voice
        accs = voice.decided_calls("ladder", 20260903, 1.0, 1200.0, 25)
        ids = {a.account_id for a in accs}
        self.assertEqual(len(ids), len(accs))          # de-duplicated
        self.assertTrue(ids)                            # ladder does call someone

    def test_key_loading_prefers_env(self):
        import os
        from recovery import sarvam
        os.environ["SARVAM_API_KEY"] = "sk_test_from_env"
        try:
            self.assertEqual(sarvam.load_key(), "sk_test_from_env")
            self.assertTrue(sarvam.available())
        finally:
            del os.environ["SARVAM_API_KEY"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
