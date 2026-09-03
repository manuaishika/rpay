# recovery/ — a bounded revenue-recovery agent

**Razorpay Buildathon · Track 03**

> A Hinglish voice call costs **~₹12**. Most failed payments aren't worth ₹12.
> The hard part isn't dialing — it's deciding **who** to call and **when to stop**.

`recovery/` is a stdlib-only Python package (no dependencies) that models a
failed-payment book, runs a 5-stage sequential agent that spends real rupees
only when the expected recovery clears the cost, and scores it against four
fixed playbooks. Every outreach decision passes through a **hard guardrail
gate**, and an **independent auditor** re-derives the rules from the emitted
audit trail so a gate bug shows up as a counted violation instead of leaking.

```
python -m recovery                 # the scorecard
python -m recovery --stress 0.5    # halve the (assumed) voice lift and re-rank
python -m recovery --json          # machine-readable
python tests.py                    # 14 stdlib self-checks
```

## What's in the box

| module | responsibility |
|---|---|
| `core.py` | 9-way failure taxonomy split into `TRANSIENT` / `ACTION_REQUIRED`; five interventions with rupee costs (`silent_retry` ₹0.50 → `human_escalation` ₹85); a seeded synthetic ledger of 250 accounts — lognormal amounts, heavy right tail for B2B, plus tenure / prior failures / language / DNC / `contacts_last_7d` / open promise-to-pay |
| `world.py` | **stated priors** `p(recover \| reason, intervention)` — loudly flagged as ASSUMPTIONS, not data. Retrying a revoked mandate is `0.0`. Voice is gated by a `0.62` pickup rate. `--stress` scales the voice lift *only*, for sensitivity runs |
| `guardrails.py` | the **hard gate**: 09:00–19:00 contact window, permanent DNC, 3 contacts / 7 days, 2 voice attempts max, promise-to-pay suppression, a global voice-budget cap and a human-escalation capacity cap. `audit_executed()` re-derives every rule from scratch against the JSONL trail |
| `ladder.py` | the **5-stage agent**. Each stage re-scores every *compliant* channel by expected net rupees given what already failed (channel fatigue `0.72` per repeat, evidence decay `0.88` per failed attempt). Voice carries extra **option value**: a connected call that doesn't convert can still capture a dated promise-to-pay that converts later at zero spend. Stops the moment nothing clears ₹0 in expectation. Plus baselines: `retry_only`, `nudge_ladder`, `call_first`, `standard_playbook` |
| `__main__.py` | runs all five policies over the ledger, writes `audit/<policy>.jsonl`, prints recovered / spend / net / rate / calls / PTPs / cost-per-₹100 and **guardrail violations (must be 0)** |

## The assumptions (read this)

Everything in `world.py` is invented. The pickup rate, every recovery
probability, the promise-to-pay conversion — none of it is fit to a real
ledger. They are internally consistent and directionally defensible, and
that is all. The `--stress` knob exists because the voice economics are the
fragile hinge of the whole argument: scale the voice lift down and watch
whether the sequential agent still beats `standard_playbook`.

## Representative run (`--seed 20260903`)

```
policy                 recovered     spend           net    rate  calls   PTP   Rs/100  viol
--------------------------------------------------------------------------------------------
ladder                 2,541,483     3,609     2,537,874   42.8%    100    17     0.14     0
retry_only             1,442,605       329     1,442,276   23.2%      0     0     0.02     0
nudge_ladder             379,615       116       379,499   16.4%      0     0     0.03     0
call_first               706,044     1,251       704,792   18.4%    100    15     0.18     0
standard_playbook      2,164,008     1,781     2,162,227   36.8%     96    16     0.08     0
```

Across seeds the sequential agent wins on net in most runs and is always far
ahead on cost-per-₹100 recovered; `standard_playbook` occasionally matches it
on net by spending nearly as much blind outreach. `call_first` burns the
voice budget on everyone and recovers a third as much. Guardrail violations
are `0` for every policy at every stress level — that is the point of the
independent auditor, not a lucky outcome.

## Audit trail

One JSONL file per policy under `audit/`. Event types: `episode_start`,
`blocked` (a channel the gate refused, with the `rule`), `action` (with
`expected_net`, `cost`, `outcome`), `ptp_created`, `ptp_resolved`,
`stop` (with `cause`), `episode_end`.
