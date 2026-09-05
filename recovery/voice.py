"""voice.py -- turn the calls the agent DECIDED to make into real Hinglish audio.

    python -m recovery.voice                 # render up to 10 of ladder's calls
    python -m recovery.voice --limit 3 --dry-run   # scripts only, no TTS / no key

Pipeline per account:
    ladder decides voice_call  ->  sarvam-105b writes a Hinglish script
                               ->  bulbul TTS renders it to a .wav

This never places a phone call. The ledger is synthetic; there is no real
number to dial and no consent to do so. The output is an audio file that
demonstrates what the voice intervention would actually sound like, and how
the script changes with the failure reason.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core
from .ladder import run_policy
from .guardrails import DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET

# What the customer actually has to DO, per failure reason -- the script must
# say this plainly instead of vaguely asking them to "try again".
REASON_ASK = {
    "insufficient_funds": "account mein balance aane par hum dobara try karenge, ya aap ek date bata dijiye jab payment ho sakta hai",
    "bank_downtime": "aapke bank ki taraf se dikkat thi; hum thodi der baad apne aap retry karenge, aapko kuch nahi karna",
    "technical_decline": "ek technical decline aaya tha; ek naye link se payment try kijiye",
    "limit_exceeded": "transaction limit cross ho gayi thi; limit badha kar ya kal dobara try kijiye",
    "do_not_honour": "bank ne transaction decline kiya; ek baar apne bank se confirm kar lijiye aur phir se try kijiye",
    "mandate_expired": "aapka auto-pay mandate expire ho gaya hai; ise renew karne ke liye ek link bhej rahe hain",
    "mandate_revoked": "aapka mandate cancel ho chuka hai; naya mandate set karne ke liye ek link bhej rahe hain",
    "card_expired": "aapke card ki validity khatam ho gayi hai; naya card add karne ke liye link bhej rahe hain",
    "invoice_overdue": "aapka invoice due ho gaya hai; payment link bhej rahe hain, aap aaj kar dijiye toh accha rahega",
    "checkout_abandoned": "aapne cart mein items add kiye the par checkout complete nahi hua; ek click checkout link bhej rahe hain",
}

# Bulbul v3 supported language codes (docs.sarvam.ai text-to-speech/convert):
#   bn-IN en-IN gu-IN hi-IN kn-IN ml-IN mr-IN od-IN pa-IN ta-IN te-IN
# name -> (TTS language_code, how the script should read). v3 speakers are
# multilingual, so one speaker works across all of these.
LANG = {
    "hinglish": ("hi-IN", "natural Hinglish (Roman + Devanagari, the way urban Indians talk)"),
    "hindi":    ("hi-IN", "natural conversational Hindi in Devanagari"),
    "english":  ("en-IN", "clear, warm Indian English"),
    "tamil":    ("ta-IN", "natural conversational Tamil"),
    "telugu":   ("te-IN", "natural conversational Telugu"),
    "marathi":  ("mr-IN", "natural conversational Marathi"),
    "bengali":  ("bn-IN", "natural conversational Bengali"),
}
DEFAULT_LANG = "hinglish"
# what the console dropdown may force for a whole run (a subset, all TTS-supported)
FORCEABLE_LANGS = ("hinglish", "hindi", "english")


def resolve_language(account, choice: str | None = None) -> str:
    """None / 'match' -> the account's own language field; else the forced choice.
    Always returns a key that exists in LANG."""
    if choice and choice != "match":
        return choice if choice in LANG else DEFAULT_LANG
    return account.language if account.language in LANG else DEFAULT_LANG


SYSTEM_PROMPT_TMPL = (
    "You are a polite, professional payment-recovery voice assistant for a "
    "business that uses Razorpay. You call customers whose payment just failed. "
    "Speak {style}. "
    "Rules: stay respectful and calm, never threaten, never mention legal action "
    "or credit score, never shame the customer. Keep it to 3-4 short sentences. "
    "Open with a one-line greeting and who you are, state the failed amount and "
    "the reason in plain terms, tell them the ONE thing that fixes it, and offer "
    "to note a specific date they can pay (promise-to-pay). End warmly. "
    "Output ONLY the words to be spoken -- no stage directions, no name "
    "placeholders like [Name], no markdown."
)


def build_messages(account, language: str | None = None) -> list[dict]:
    lang_key = resolve_language(account, language)
    _, style = LANG[lang_key]
    amt = f"Rs.{account.amount:,.0f}"
    ask = REASON_ASK.get(account.reason, "payment dobara try kijiye")
    kind = "business (B2B)" if account.segment == "B2B" else "individual (B2C)"
    user = (
        f"Customer type: {kind}. Failed amount: {amt}. "
        f"Failure reason: {account.reason}. "
        f"What fixes it (convey this in your own words, in {style}): {ask}. "
        f"Write the call script now."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT_TMPL.format(style=style)},
        {"role": "user", "content": user},
    ]


def _fallback_script(account, language: str | None = None) -> str:
    amt = f"Rs.{account.amount:,.0f}"
    ask = REASON_ASK.get(account.reason, "payment dobara try kijiye")
    if resolve_language(account, language) == "english":
        return (
            f"Hello, this is the Razorpay recovery team. "
            f"Your payment of {amt} did not go through ({account.reason.replace('_', ' ')}). "
            f"We've sent you a link to sort it out. "
            f"If you can share a date you will pay, I'll note it and we'll try again. "
            f"Thank you for your time."
        )
    return (
        f"Namaste, main Razorpay ki recovery team se baat kar raha hoon. "
        f"Aapka {amt} ka payment abhi complete nahi ho paaya. "
        f"{ask.capitalize()}. "
        f"Aap ek date bata dijiye toh main note kar leta hoon, phir dobara koshish karenge. "
        f"Time dene ke liye dhanyavaad."
    )


def decided_calls(policy: str, seed: int, stress: float, voice_budget: float,
                  human_cap: int) -> list:
    """Accounts the policy actually chose to voice-call, in order, de-duplicated."""
    ledger = core.build_ledger(seed)
    _, events = run_policy(policy, ledger, seed, stress, voice_budget, human_cap)
    by_id = {a.account_id: a for a in ledger}
    seen, out = set(), []
    for ev in events:
        if ev.get("event") == "action" and ev.get("intervention") == "voice_call":
            aid = ev["account_id"]
            if aid not in seen:
                seen.add(aid)
                out.append(by_id[aid])
    return out


def render(account, client, out_dir: Path, lang_choice: str, speaker: str,
           dry_run: bool) -> dict:
    lang_key = resolve_language(account, lang_choice)
    tts_code, _ = LANG[lang_key]
    if client is not None:
        try:
            script = client.chat(build_messages(account, lang_choice))
        except Exception as e:                       # noqa: BLE001 - log and fall back
            print(f"  ! {account.account_id}: script generation failed ({e}); using fallback")
            script = _fallback_script(account, lang_choice)
    else:
        script = _fallback_script(account, lang_choice)

    stem = out_dir / account.account_id
    meta = {
        "account_id": account.account_id,
        "segment": account.segment,
        "reason": account.reason,
        "amount": round(account.amount, 2),
        "language": lang_key,
        "language_code": tts_code,
        "speaker": speaker,
        "script": script,
        "chars": len(script),
        "audio_file": None,
    }
    stem.with_suffix(".txt").write_text(script + "\n", encoding="utf-8")

    if not dry_run and client is not None:
        try:
            wav = client.text_to_speech(script, language_code=tts_code, speaker=speaker)
            stem.with_suffix(".wav").write_bytes(wav)
            meta["audio_file"] = stem.with_suffix(".wav").name
            meta["audio_bytes"] = len(wav)
        except Exception as e:                       # noqa: BLE001
            print(f"  ! {account.account_id}: TTS failed ({e})")

    stem.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m recovery.voice")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--stress", type=float, default=1.0)
    ap.add_argument("--voice-budget", type=float, default=DEFAULT_VOICE_BUDGET)
    ap.add_argument("--human-cap", type=int, default=DEFAULT_HUMAN_CAP)
    ap.add_argument("--policy", default="ladder")
    ap.add_argument("--limit", type=int, default=10, help="max calls to render")
    ap.add_argument("--out", default="audit/calls")
    ap.add_argument("--language", default="match",
                    help="match (each account's own) | " + " | ".join(LANG))
    ap.add_argument("--speaker", default="priya")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate scripts only: no TTS, no API key needed")
    args = ap.parse_args(argv)

    from . import sarvam
    client = None
    if not args.dry_run:
        if not sarvam.available():
            print("no Sarvam API key found.\n"
                  "  set it:   export SARVAM_API_KEY=sk_...\n"
                  "  or file:  echo 'SARVAM_API_KEY=sk_...' > recovery/.env\n"
                  "  or run:   python -m recovery.voice --dry-run", file=sys.stderr)
            return 2
        client = sarvam.Sarvam()

    accounts = decided_calls(args.policy, args.seed, args.stress,
                             args.voice_budget, args.human_cap)[: args.limit]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"policy={args.policy} seed={args.seed}: "
          f"{len(accounts)} call(s) to render into {out_dir}/"
          + ("  [dry run: scripts only]" if args.dry_run else ""))
    metas = [render(a, client, out_dir, args.language, args.speaker, args.dry_run)
             for a in accounts]

    print(f"\n{'account':<12}{'segment':<9}{'reason':<20}{'lang':<10}{'amount':>12}  {'audio':>10}")
    print("-" * 76)
    for m in metas:
        audio = m.get("audio_file") or ("-" if args.dry_run else "FAILED")
        print(f"{m['account_id']:<12}{m['segment']:<9}{m['reason']:<20}{m['language']:<10}"
              f"{'Rs.' + format(m['amount'], ',.0f'):>12}  {audio:>10}")
    print(f"\nwrote {len(metas)} script(s)"
          + ("" if args.dry_run else f" + audio to {out_dir.resolve()}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
