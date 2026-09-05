"""recovery/ — a bounded revenue-recovery agent.

Thesis: a Hinglish voice call costs ~Rs.5.44 -- DERIVED from Sarvam's
published rates (see recovery/costs.py), not guessed. At that price plenty
of failed payments ARE worth a call, so the hard problem is deciding WHICH
ones, in what order, against a fixed budget -- and knowing WHEN TO STOP.
This package is stdlib-only and fully deterministic from a seed.

Modules
-------
core        failure taxonomy, intervention costs, synthetic account ledger
world       STATED PRIORS for p(recover | reason, intervention) -- ASSUMPTIONS
guardrails  a HARD GATE (not a prompt) + an independent post-hoc auditor
ladder      the 5-stage sequential agent + fixed-playbook baselines
__main__    run every policy over the ledger and print the scorecard
"""

__all__ = ["core", "world", "guardrails", "ladder"]
