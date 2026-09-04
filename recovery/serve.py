"""serve.py -- the operator console. stdlib http.server, no dependencies.

    python -m recovery.serve            # then open http://127.0.0.1:8000

Routes
------
GET  /                        the console UI (web/console.html)
GET  /api/book?seed=&n=       the failed-payment book + a chosen live sample
POST /api/run                 NDJSON stream: the agent working the sample live,
                              account by account, then the fixed-playbook
                              benchmark on the same accounts, then a scorecard
POST /api/render_call         {account_id, seed}: Hinglish script for one account
                              (+ .wav bytes as base64 if a Sarvam key is present)

The agent is recovery/agent.py (sarvam-105b in the decision seat, guardrail
gate around it). No key -> it falls back to the expected-value rule and the
console still runs; every decision is tagged with who made it.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import core, world
from .agent import run_agent_episode
from .analysis import policy_metrics
from .guardrails import DEFAULT_HUMAN_CAP, DEFAULT_VOICE_BUDGET, audit_executed
from .ladder import run_policy_on

WEB = Path(__file__).resolve().parent.parent / "web" / "console.html"
BENCHMARKS = ["standard_playbook", "retry_only"]
MAX_SAMPLE = 40

# One /api/run at a time. This is a demo server behind a shared, metered
# Sarvam key -- once it has a public URL, this is what stops a second
# visitor's click (or a reload) from stacking concurrent LLM runs on it.
_RUN_LOCK = threading.Lock()


def pick_sample(ledger, n: int, seed: int):
    """A spread worth watching: the biggest exposures, the awkward cases, then
    a seeded random fill -- so the demo shows range, deterministically."""
    n = max(3, min(int(n), MAX_SAMPLE, len(ledger)))
    by_amt = sorted(ledger, key=lambda a: -a.amount)
    chosen, seen = [], set()

    def take(acc):
        if acc.account_id not in seen and len(chosen) < n:
            seen.add(acc.account_id)
            chosen.append(acc)

    for a in by_amt[:3]:
        take(a)
    for a in ledger:
        if a.reason in ("mandate_revoked", "mandate_expired") and len(chosen) < n // 3 + 3:
            take(a)
    for a in ledger:
        if a.dnc:
            take(a)
    rest = [a for a in ledger if a.account_id not in seen]
    random.Random(seed).shuffle(rest)
    for a in rest:
        take(a)
    return sorted(chosen, key=lambda a: a.account_id)


def _acc_view(a):
    return {
        "account_id": a.account_id, "segment": a.segment, "reason": a.reason,
        "amount": round(a.amount, 2), "tenure_months": a.tenure_months,
        "prior_failures": a.prior_failures, "language": a.language,
        "dnc": a.dnc, "has_phone": a.has_phone,
        "open_ptp": a.promise_to_pay_due,
        "reason_class": "transient" if a.reason in core.TRANSIENT else "action_required",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):            # quiet
        pass

    # -- helpers -------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _begin_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _line(self, obj):
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise _ClientGone()

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # -- routes -------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                self._send(200, WEB.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"console.html not found next to the package")
            return
        if u.path == "/api/book":
            q = parse_qs(u.query)
            seed = int(q.get("seed", ["20260903"])[0])
            n = int(q.get("n", ["16"])[0])
            ledger = core.build_ledger(seed)
            sample = pick_sample(ledger, n, seed)
            payload = {
                "seed": seed,
                "summary": core.ledger_summary(ledger),
                "interventions": core.INTERVENTION_COST,
                "voice_pickup_rate": world.VOICE_PICKUP_RATE,
                "defaults": {"voice_budget": DEFAULT_VOICE_BUDGET, "human_cap": DEFAULT_HUMAN_CAP},
                "sample": [_acc_view(a) for a in sample],
            }
            self._send(200, json.dumps(payload).encode("utf-8"))
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/run":
            self._run()
        elif u.path == "/api/render_call":
            self._render_call()
        else:
            self._send(404, b'{"error":"not found"}')

    # -- /api/run ---------------------------------------------------
    def _run(self):
        if not _RUN_LOCK.acquire(blocking=False):
            self._send(429, json.dumps({
                "error": "a recovery run is already in progress on this server; wait for it "
                         "to finish (the console disables the button while running)."
            }).encode("utf-8"))
            return
        try:
            self._run_locked()
        finally:
            _RUN_LOCK.release()

    def _run_locked(self):
        p = self._body()
        seed = int(p.get("seed", 20260903))
        stress = float(p.get("stress", 1.0))
        vb = float(p.get("voice_budget", DEFAULT_VOICE_BUDGET))
        hc = int(p.get("human_cap", DEFAULT_HUMAN_CAP))
        use_llm = bool(p.get("use_llm", True))
        n = int(p.get("sample", 16))

        ledger = core.build_ledger(seed)
        sample = pick_sample(ledger, n, seed)

        client = None
        client_note = "expected-value rule (no LLM requested)"
        if use_llm:
            try:
                from .sarvam import Sarvam, available
                if available():
                    client = Sarvam(timeout=40)
                    client_note = "sarvam-105b in the decision seat"
                else:
                    client_note = "no Sarvam key found -> expected-value rule fallback"
            except Exception as e:                              # noqa: BLE001
                client_note = f"Sarvam unavailable ({e}) -> rule fallback"

        self._begin_stream()
        try:
            self._line({"event": "run_start", "sample": [_acc_view(a) for a in sample],
                        "voice_budget": vb, "human_cap": hc, "agent": client_note})

            from .guardrails import Guardrails
            guard = Guardrails(sample, voice_budget=vb, human_cap=hc)
            now0 = guard.now0.replace(hour=0)
            q: queue.Queue = queue.Queue()
            results = {}

            def work(acc):
                rng = random.Random(f"{seed}:{acc.account_id}")
                ep, evs = run_agent_episode(acc, guard, rng, stress, client, now0,
                                            emit=lambda e: q.put(e))
                results[acc.account_id] = (ep, evs)
                q.put({"event": "account_done", "account_id": acc.account_id})

            workers = 5 if client is not None else 12
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            futs = [pool.submit(work, a) for a in sample]
            done_flag = {"n": 0}

            def watch():
                concurrent.futures.wait(futs)
                q.put({"event": "__all_done__"})
            threading.Thread(target=watch, daemon=True).start()

            while True:
                ev = q.get()
                if ev.get("event") == "__all_done__":
                    break
                if ev.get("event") == "account_done":
                    done_flag["n"] += 1
                    ev["progress"] = [done_flag["n"], len(sample)]
                self._line(ev)
            pool.shutdown(wait=True)

            # ---- agent scorecard on the sample ----
            agent_eps = [results[a.account_id][0] for a in sample]
            agent_evs = [e for a in sample for e in results[a.account_id][1]]
            agent_v = audit_executed(agent_evs, sample, vb, hc)
            total_v = len(agent_v)
            self._line({"event": "scorecard", "policy": "agent",
                        "metrics": policy_metrics("agent", agent_eps, agent_evs, agent_v, sample)})

            # ---- fixed-playbook benchmark on the SAME accounts ----
            for name in BENCHMARKS:
                eps, evs = run_policy_on(name, sample, seed, stress, vb, hc)
                v = audit_executed(evs, sample, vb, hc)
                total_v += len(v)
                self._line({"event": "scorecard", "policy": name,
                            "metrics": policy_metrics(name, eps, evs, v, sample)})

            self._line({"event": "done", "violations": total_v})
        except _ClientGone:
            pass

    # -- /api/render_call -----------------------------------------
    def _render_call(self):
        p = self._body()
        seed = int(p.get("seed", 20260903))
        aid = str(p.get("account_id", ""))
        ledger = {a.account_id: a for a in core.build_ledger(seed)}
        acc = ledger.get(aid)
        if not acc:
            self._send(404, b'{"error":"unknown account"}')
            return
        from .voice import build_messages, _fallback_script
        out = {"account_id": aid, "reason": acc.reason, "amount": round(acc.amount, 2),
               "segment": acc.segment, "audio_b64": None}
        try:
            from .sarvam import Sarvam, available
            if available():
                c = Sarvam(timeout=40)
                out["script"] = c.chat(build_messages(acc))
                try:
                    import base64
                    wav = c.text_to_speech(out["script"], language_code="hi-IN")
                    out["audio_b64"] = base64.b64encode(wav).decode("ascii")
                except Exception as e:                          # noqa: BLE001
                    out["tts_error"] = str(e)[:120]
            else:
                out["script"] = _fallback_script(acc)
                out["note"] = "no Sarvam key -> template script, no audio"
        except Exception as e:                                  # noqa: BLE001
            out["script"] = _fallback_script(acc)
            out["error"] = str(e)[:120]
        self._send(200, json.dumps(out).encode("utf-8"))


class _ClientGone(Exception):
    pass


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m recovery.serve")
    # $PORT is how Render (and most PaaS hosts) tell the process which port
    # the public router expects; --port always wins if you pass it explicitly.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = ap.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"recovery console -> http://{args.host}:{args.port}")
    print("  Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
