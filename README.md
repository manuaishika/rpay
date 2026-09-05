# Vasool — a bounded revenue-recovery agent

**Razorpay Buildathon · Track 03, "AI Revenue Recovery"**

> Merchants today either retry blindly or contact everyone. Neither *decides*. This
> allocates a fixed contact budget across accounts under a compliance policy, and shows
> what that's worth against both.
>
> Built entirely on free tiers — Sarvam's ₹100 credits cover the whole demo. At
> production rates a connected voice call costs **~₹5.44**, [itemised](recovery/costs.py):
> STT, TTS, LLM from Sarvam's published rates, telephony estimated. That's the number
> the agent budgets against.

`recovery/` is a stdlib-only Python package (no dependencies) that models
revenue at risk across the three points the brief names — a stalled
**checkout**, a failed **payment**, an overdue **receivable** — and puts an
agent in the decision seat: for each one it weighs the actions the guardrail
gate currently permits and either spends or stops. Every decision passes
through a **hard guardrail gate**, and an **independent auditor** re-derives
the rules from the emitted audit trail so a gate bug shows up as a counted
violation instead of leaking.

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
python -m recovery.sweep           # stress x voice-cost grid + per-account decision boundary
python -m recovery.voice --limit 5 # render the Hinglish scripts + .wav via Sarvam
python -m recovery.costs           # the voice-cost derivation, itemised
python -m recovery.assumptions     # regenerate ASSUMPTIONS.md from the register
python tests.py                    # stdlib self-checks
```

**Static interactive scorecard (no server):** <https://claude.ai/code/artifact/ad74a183-90ee-4c8e-8721-bcc97845d488>

## What's in the box

| module | responsibility |
|---|---|
| `costs.py` | **derives** the voice-call cost (₹~5.44 / connected call) from Sarvam's published STT/TTS/LLM rates plus estimated telephony and call shape — every input marked `SOURCED` (URL) or `ESTIMATED`. Failed-dial telephony is spread over the pickup rate |
| `core.py` | 10-way revenue-at-risk taxonomy spanning checkout / payment / receivable (`risk_stage()`), split into `TRANSIENT` / `ACTION_REQUIRED`; five interventions, `voice_call` cost from `costs.py`, the rest estimated; a seeded synthetic ledger of 250 accounts — lognormal amounts, heavy right tail for B2B, plus tenure / prior failures / language / DNC / `contacts_last_7d` / open promise-to-pay |
| `world.py` | **stated priors** `p(recover \| reason, intervention)` — loudly flagged as ASSUMPTIONS, not data. Retrying a revoked mandate is `0.0`. Voice is gated by a `0.62` pickup rate. `--stress` scales the voice lift *only*, for sensitivity runs |
| `assumptions.py` | the input register — one row per number, `SOURCED` / `ESTIMATED`, and what would replace it. `python -m recovery.assumptions` regenerates `ASSUMPTIONS.md`; every eval prints its `header()` |
| `guardrails.py` | the **hard gate**: a tunable `ContactPolicy` (contact window, rolling frequency cap, voice-attempt ceiling), permanent DNC, promise-to-pay suppression, a global voice-budget cap and a human-escalation capacity cap. `audit_executed()` re-derives every rule from scratch against the JSONL trail |
| `ladder.py` | the **expected-value policy** + the fixed-playbook baselines. Each stage re-scores every *permitted* channel by expected net rupees given what already failed (channel fatigue `0.72` per repeat, evidence decay `0.88` per failed attempt). Voice carries extra **option value**: a connected call that doesn't convert can still capture a dated promise-to-pay that converts later at zero spend. Stops the moment nothing clears ₹0 in expectation. Baselines: `retry_only`, `nudge_ladder`, `call_first`, `standard_playbook` |
| `agent.py` | **`sarvam-105b` in the decision seat.** Same environment, same gate, same 5-stage bound — but the model picks the next action (from the guardrail-permitted menu only) and says *why*, given the account, what already failed, the costs, the believed odds, and the budget left. Falls back to the expected-value rule if there's no key or the call fails; every decision is tagged with its source |
| `serve.py` + `web/console.html` | the **operator console** — stdlib `http.server`, streams the agent working the book live over NDJSON, then the playbook benchmark, then the scorecard |
| `__main__.py` | runs all five policies over the ledger, writes `audit/<policy>.jsonl`, prints recovered / spend / net / rate / calls / PTPs / cost-per-₹100 and **guardrail violations (must be 0)** |
| `analysis.py` | shared scoring + cohort slicing (B2B/B2C, by reason); used by the CLI, the sweep, and the dashboard |
| `sweep.py` | runs the whole `stress × voice-cost` grid, all policies per cell, and reports the crossover where the ladder stops beating the best fixed playbook |
| `sarvam.py` · `voice.py` | *optional.* Take the calls the `ladder` actually decided to make, generate a reason-specific Hinglish script (`sarvam-105b`), render it to a `.wav` (`bulbul` TTS). Degrades to script-only with no API key. Never places a real call |
| `dashboard.py` · `build_dashboard.py` | assemble `audit/dashboard.json` from a real run and bake it into a standalone `dashboard.html` (the interactive scorecard linked above) |

## What this measures — and what it does not

**By default the agent is given the true outcome model.** `ladder.py` scores
channels with `world.p_recover()`; the runner resolves outcomes with the same
function. So the headline result measures whether **budget-aware triage
allocates a fixed contact budget better than fixed playbooks** — not whether a
learned model could estimate those probabilities from real data. The
playbooks are blind to the recovery odds; the agent is not.

`--sigma` decouples them (see [Misspecification](#misspecification)): the agent
scores on `p̂ = p · lognormal(0, σ)` (mean-preserving, seeded apart from the
outcome RNG) while the world still resolves on `p`. **The ranking survives** —
the ladder still beats `standard_playbook` in 4 of 5 seeds at σ up to 0.6,
mean margin holding around +₹300k. That's a stronger claim than "arithmetic
beats no arithmetic": budget-aware triage wins even when the probabilities it
triages on are wrong.

The 58% recovery rate on a live console sample (41% over the full book)
reads high for real dunning — because the ledger is **synthetic**, generated
to be internally plausible, not sampled from a real book.

## The assumptions register

Most of the numbers this system depends on are estimates, not sourced data —
every recovery probability in `world.py`, the pickup rate, the promise-to-pay
conversions, the ledger distributions. **[`ASSUMPTIONS.md`](ASSUMPTIONS.md)**
is the full register: one row per input, marked `SOURCED` (with a URL) or
`ESTIMATED`, and what it would take to replace it with real data. Every run
prints a one-line header with the sourced-vs-estimated count.

The `--stress` knob exists because the voice economics are the fragile hinge:
scale the voice lift down and watch whether the sequential agent still beats
`standard_playbook`.

### The contact policy is not a compliance claim

The gate enforces a **configurable contact policy** (`ContactPolicy` in
`guardrails.py`) — a 09:00–19:00 window, 3 contacts per rolling 7 days, 2
voice attempts per account. These are conservative defaults **chosen by the
author, not taken from any RBI or TRAI circular.** Mapping them to the actual
RBI recovery-agent guidelines and TRAI UCC/DND requirements is required
before production use. What the auditor proves is that the agent obeyed
*whatever policy it was given*, not that the policy is lawful.

## Representative run (`--seed 20260903`, voice cost derived at ₹5.44)

```
policy                 recovered     spend           net    rate  calls   PTP   Rs/100  viol
--------------------------------------------------------------------------------------------
ladder                 2,418,139     3,531     2,414,608   41.2%    220    35     0.15     0
retry_only               509,786       342       509,443   20.0%      0     0     0.07     0
nudge_ladder             302,075       116       301,959   16.8%      0     0     0.04     0
call_first             1,167,199     1,235     1,165,964   22.8%    220    38     0.11     0
standard_playbook      2,033,189     1,304     2,031,885   32.4%    107    17     0.06     0
```

`retry_only`'s net is a fifth of the ladder's: a fifth of the book is
`checkout_abandoned` (nothing was ever attempted, so a silent retry is
structurally unable to help), and blind retrying spends money on all of it.
The ladder scores that at zero and stops. Guardrail violations are `0` for
every policy in every one of the 70 sweep cells — that is the point of the
independent auditor, not a lucky outcome.

### What the cheaper call cost changes

At the old ₹12 guess the agent made ~100 calls and the question was largely
"is any call worth it". At the derived ₹5.44 a lot more calls clear zero, so
the agent makes ~220 (the ₹1,200 voice budget divided by ₹5.44) and the job
shifts to **ranking** — spend the marginal rupee on the highest-expected-net
account, and pick a cheaper channel when one wins. The sweep shows the
selectivity mattering *more* as the cost rises: the ladder's lead over
`standard_playbook` widens sharply past ₹12/call, where the blind playbook
keeps dialling at stage 4 and the ladder pulls back.

### The decision boundary (`python -m recovery.sweep`)

For representative accounts, the voice cost at which the stage-1 pick leaves
voice for a cheaper channel:

| reason | amount | best non-voice net | prefers voice at ₹5.44? | flips at |
|---|---|---|---|---|
| insufficient_funds | ₹500 | ₹74 | yes | ₹29.75 |
| insufficient_funds | ₹2,000 | ₹300 | yes | > ₹30 |
| card_expired | ₹1,500 | ₹485 | yes | > ₹30 |
| mandate_revoked | ₹40,000 | ₹8,715 | yes | > ₹30 |
| invoice_overdue | ₹6,000 | ₹2,795 | **no** | ₹1 |
| checkout_abandoned | ₹1,500 | ₹300 | **no** | ₹1 |

`invoice_overdue` and `checkout_abandoned` never prefer a voice call at any
realistic cost — a human touch or a link already beats it. Everything else
does, and stays that way well past the derived cost.

### Misspecification

`python -m recovery --sigma 0.35` — the agent scores on a noised estimate,
the world resolves on truth. Across 5 seeds:

| σ | ladder still beats `standard_playbook` | mean net margin | worst seed |
|---|---|---|---|
| 0.0 (answer key) | 4 / 5 | +₹308k | −₹151k |
| 0.35 | 4 / 5 | +₹388k | −₹96k |
| 0.6 | 4 / 5 | +₹266k | −₹351k |

The one seed the ladder loses, it loses at σ=0 too — noise doesn't create new
losses, and the average lead is flat across noise levels. So the win is about
*allocating a budget by rank*, not about having exact probabilities. The one
seed it loses on is the honest caveat: on a book heavy with a few very large
accounts, a conservative playbook that dials fewer times can come out ahead.

## Audit trail

One JSONL file per policy under `audit/`. Event types: `episode_start`,
`blocked` (a channel the gate refused, with the `rule`), `action` (with
`expected_net`, `cost`, `outcome`), `ptp_created`, `ptp_resolved`,
`stop` (with `cause`), `episode_end`.

## Hearing the call (optional — Sarvam AI)

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
