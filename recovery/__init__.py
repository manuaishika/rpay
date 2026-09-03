"""recovery/ — a bounded revenue-recovery agent.

Thesis: a Hinglish voice call costs ~Rs.12. Most failed payments are not
worth Rs.12. The hard problems are deciding WHO to call and knowing WHEN
TO STOP. This package is stdlib-only and fully deterministic from a seed.

Modules
-------
core        failure taxonomy, intervention costs, synthetic account ledger
world       STATED PRIORS for p(recover | reason, intervention) -- ASSUMPTIONS
guardrails  a HARD GATE (not a prompt) + an independent post-hoc auditor
ladder      the 5-stage sequential agent + fixed-playbook baselines
__main__    run every policy over the ledger and print the scorecard
"""

__all__ = ["core", "world", "guardrails", "ladder"]
