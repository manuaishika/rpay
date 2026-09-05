# recovery/ — a bounded revenue-recovery agent

**Razorpay Buildathon · Track 03**

> A Hinglish voice call costs **~₹12**. Most failed payments aren't worth ₹12.
> The hard part isn't dialing — it's deciding **who** to call and **when to stop**.

`recovery/` is a stdlib-only Python package (no dependencies) that models a
failed-payment book and puts an agent in the decision seat: for each failed
payment it weighs the actions the guardrail gate currently permits and either
spends or stops. Every decision passes through a **hard guardrail gate**, and
an **independent auditor** re-derives the rules from the emitted audit trail
so a gate bug shows up as a counted violation instead of leaking.

## Run it — the console

```
python -m recovery.serve      # then open http://127.0.0.1:8000
```

`/` is a short landing page (what it is, how it decides); `/console` is the
tool itself. Set the voice budget and how many accounts to run live, hit **Run recovery**,
and watch `sarvam-105b` work each failed payment — its pick, its reasoning,
guardrail blocks, the budget draining, promises-to-pay logged — then a
scorecard against the fixed playbooks (`agent` vs `standard_playbook` vs
`retry_only`), with **guardrail violations: 0**. Click any account for its
full trace; if it placed a call, hear the Hinglish (script + audio).

No Sarvam key → the console falls back to the expected-value rule and still
runs; every decision is tagged with who made it (`llm` / `rule`).

## Run it — the batch analysis

```
python -m recovery                 # scorecard over the full 250-account book
python -m recovery --cohorts       # + B2B/B2C and by-reason breakdown
python -m recovery --stress 0.5    # halve the (assumed) voice lift and re-rank
python -m recovery.sweep           # stress x voice-cost grid; where the ladder stops winning
python -m recovery.voice --limit 5 # render the Hinglish scripts + .wav via Sarvam
python tests.py                    # 25 stdlib self-checks
```

**Static interactive scorecard (no server):** <https://claude.ai/code/artifact/ad74a183-90ee-4c8e-8721-bcc97845d488>

## What's in the box

| module | responsibility |
|---|---|
| `core.py` | 9-way failure taxonomy split into `TRANSIENT` / `ACTION_REQUIRED`; five interventions with rupee costs (`silent_retry` ₹0.50 → `human_escalation` ₹85); a seeded synthetic ledger of 250 accounts — lognormal amounts, heavy right tail for B2B, plus tenure / prior failures / language / DNC / `contacts_last_7d` / open promise-to-pay |
| `world.py` | **stated priors** `p(recover \| reason, intervention)` — loudly flagged as ASSUMPTIONS, not data. Retrying a revoked mandate is `0.0`. Voice is gated by a `0.62` pickup rate. `--stress` scales the voice lift *only*, for sensitivity runs |
| `guardrails.py` | the **hard gate**: 09:00–19:00 contact window, permanent DNC, 3 contacts / 7 days, 2 voice attempts max, promise-to-pay suppression, a global voice-budget cap and a human-escalation capacity cap. `audit_executed()` re-derives every rule from scratch against the JSONL trail |
| `ladder.py` | the **expected-value policy** + the fixed-playbook baselines. Each stage re-scores every *compliant* channel by expected net rupees given what already failed (channel fatigue `0.72` per repeat, evidence decay `0.88` per failed attempt). Voice carries extra **option value**: a connected call that doesn't convert can still capture a dated promise-to-pay that converts later at zero spend. Stops the moment nothing clears ₹0 in expectation. Baselines: `retry_only`, `nudge_ladder`, `call_first`, `standard_playbook` |
| `agent.py` | **`sarvam-105b` in the decision seat.** Same environment, same gate, same 5-stage bound — but the model picks the next action (from the guardrail-permitted menu only) and says *why*, given the account, what already failed, the costs, the believed odds, and the budget left. Falls back to the expected-value rule if there's no key or the call fails; every decision is tagged with its source |
| `serve.py` + `web/console.html` | the **operator console** — stdlib `http.server`, streams the agent working the book live over NDJSON, then the playbook benchmark, then the scorecard |
| `__main__.py` | runs all five policies over the ledger, writes `audit/<policy>.jsonl`, prints recovered / spend / net / rate / calls / PTPs / cost-per-₹100 and **guardrail violations (must be 0)** |
| `analysis.py` | shared scoring + cohort slicing (B2B/B2C, by reason); used by the CLI, the sweep, and the dashboard |
| `sweep.py` | runs the whole `stress × voice-cost` grid, all policies per cell, and reports the crossover where the ladder stops beating the best fixed playbook |
| `sarvam.py` · `voice.py` | *optional.* Take the calls the `ladder` actually decided to make, generate a reason-specific Hinglish script (`sarvam-105b`), render it to a `.wav` (`bulbul` TTS). Degrades to script-only with no API key. Never places a real call |
| `dashboard.py` · `build_dashboard.py` | assemble `audit/dashboard.json` from a real run and bake it into a standalone `dashboard.html` (the interactive scorecard linked above) |

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
ladder                 2,459,020     3,612     2,455,408   41.2%    100    18     0.15     0
retry_only             1,442,605       329     1,442,276   23.2%      0     0     0.02     0
nudge_ladder             379,615       116       379,499   16.4%      0     0     0.03     0
call_first               605,925     1,254       604,671   16.0%    100    15     0.21     0
standard_playbook      2,095,536     1,781     2,093,755   36.4%     96    18     0.09     0
```

Across seeds the sequential agent wins on net in most runs and is always far
ahead on cost-per-₹100 recovered; `standard_playbook` occasionally matches it
on net by spending nearly as much blind outreach. `call_first` burns the
voice budget on everyone and recovers a third as much. Guardrail violations
are `0` for every policy at every stress level — that is the point of the
independent auditor, not a lucky outcome.

### Where the "don't call" discipline actually comes from

A connected call has positive expected value for almost any amount above
roughly ₹40–₹90 (depends on reason and `--stress`), so a purely per-call EV
test would call nearly everyone. The bound that matters is the **portfolio
voice budget**: ~100 calls for 250 accounts forces the agent to *rank* by
expected net and spend the marginal rupee only where it beats the
alternative. That, plus the guardrails and the stage-5 stop, is "when to
stop". The agent still declines to call the doomed cases outright — a
revoked mandate scores `0.0` on retry and near-zero elsewhere, so a small
B2C revoked-mandate failure gets a nudge at most and then a `stop` event.

## Audit trail

One JSONL file per policy under `audit/`. Event types: `episode_start`,
`blocked` (a channel the gate refused, with the `rule`), `action` (with
`expected_net`, `cost`, `outcome`), `ptp_created`, `ptp_resolved`,
`stop` (with `cause`), `episode_end`.

## Hearing the ₹12 call (optional — Sarvam AI)

`recovery/voice.py` closes the loop between the decision and the artifact:

```
ladder decides voice_call  ->  sarvam-105b writes a reason-specific Hinglish script
                           ->  bulbul TTS renders it to audit/calls/<account>.wav
```

It runs **only** for the calls the `ladder` policy actually chose (capped by
`--limit`, default 10), so it stays cheap and on-thesis. It never dials a
phone — the ledger is synthetic, there is no consented number, and placing
real calls would need a separate telephony provider. The output is audio you
can play plus a `.json` with the script and metadata.

```bash
# needs a key: env SARVAM_API_KEY, or a gitignored recovery/.env
python -m recovery.voice --limit 5
python -m recovery.voice --dry-run          # scripts only, no key, no TTS
python -m recovery.voice --speaker rahul --language hi-IN --limit 3
```

The generated scripts are LLM output and therefore not deterministic (the
economic model in the other modules is). Example (`acc_0001`, card expired,
B2B):

> *Hello, main Razorpay se baat kar raha hoon. Aapka 52,356 rupees ka payment
> fail ho gaya hai kyunki aapke card ki validity khatam ho gayi hai. Isko fix
> karne ke liye main aapko naya card add karne ka link bhej raha hoon. Aap
> mujhe bata dijiye ki aap kis date tak payment kar denge, main wahi note kar
> leta hoon...*
