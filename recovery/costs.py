"""costs.py -- DERIVE the voice-call cost instead of hardcoding it.

Every input is marked SOURCED (with a URL) or ESTIMATED. The SOURCED rates
are Sarvam's published pricing, retrieved 2026-09:

    https://docs.sarvam.ai/api/getting-started/pricing

`voice_call_cost()` returns the cost of one CONNECTED call. Failed dials
still burn telephony, so that cost is spread back over the pickup rate --
the number the agent reasons with is per successful conversation, not per
dial.

Model: a ~1.8-minute call, 8 conversational turns, the agent speaking ~60%
of the time. With the defaults below that lands around Rs.5-6 depending
mostly on the (estimated) telephony rate.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- SOURCED: Sarvam published pricing ---------------------------------------
#     https://docs.sarvam.ai/api/getting-started/pricing   (retrieved 2026-09)
STT_RUPEES_PER_HOUR = 30.0          # SOURCED -- Saaras STT, billed per second
TTS_RUPEES_PER_10K_CHARS = 30.0     # SOURCED -- Bulbul v3 TTS
LLM_INPUT_RUPEES_PER_M = 29.28      # SOURCED -- sarvam-105b-conversations, input tokens
LLM_OUTPUT_RUPEES_PER_M = 73.20     # SOURCED -- sarvam-105b-conversations, output tokens


@dataclass(frozen=True)
class CallModel:
    """Shape of one recovery call. Everything here is ESTIMATED and tunable."""
    call_minutes: float = 1.8              # ESTIMATED -- length of a connected call
    turns: int = 8                         # ESTIMATED -- conversational turns
    agent_talk_fraction: float = 0.60      # ESTIMATED -- share of the call the agent speaks
    speech_chars_per_min: float = 900.0    # ESTIMATED -- spoken-Hinglish character density
    pickup_rate: float = 0.62              # ESTIMATED -- dials that connect (= world.VOICE_PICKUP_RATE)

    telephony_rupees_per_min: float = 0.90       # ESTIMATED -- outbound India voice via a CPaaS
    telephony_failed_dial_rupees: float = 0.50   # ESTIMATED -- a dial that rings out / no answer

    llm_instr_tokens_per_turn: float = 250.0     # ESTIMATED -- standing instructions each turn
    llm_transcript_tokens_per_turn: float = 150.0  # ESTIMATED -- one agent + one customer utterance, added to history
    llm_output_tokens_per_turn: float = 90.0     # ESTIMATED -- the agent's spoken reply + small structured tail


DEFAULT_CALL = CallModel()


def _stt_cost(m: CallModel) -> float:
    # STT runs on the customer's speech only -- the agent's words we already
    # have (we generated them), no transcription needed.
    customer_seconds = m.call_minutes * 60.0 * (1.0 - m.agent_talk_fraction)
    return STT_RUPEES_PER_HOUR * (customer_seconds / 3600.0)


def _tts_cost(m: CallModel) -> float:
    agent_chars = m.call_minutes * m.agent_talk_fraction * m.speech_chars_per_min
    return TTS_RUPEES_PER_10K_CHARS * (agent_chars / 10_000.0)


def _llm_cost(m: CallModel) -> float:
    # turn k feeds back the transcript so far, so input grows linearly with k
    total_input = sum(m.llm_instr_tokens_per_turn + k * m.llm_transcript_tokens_per_turn
                      for k in range(m.turns))
    total_output = m.turns * m.llm_output_tokens_per_turn
    return (total_input / 1_000_000.0 * LLM_INPUT_RUPEES_PER_M
            + total_output / 1_000_000.0 * LLM_OUTPUT_RUPEES_PER_M)


def _telephony_connected(m: CallModel) -> float:
    return m.telephony_rupees_per_min * m.call_minutes


def _failed_dial_amortised(m: CallModel) -> float:
    wasted_dials = (1.0 - m.pickup_rate) / m.pickup_rate
    return wasted_dials * m.telephony_failed_dial_rupees


def voice_call_cost(m: CallModel = DEFAULT_CALL) -> float:
    """Rupees per CONNECTED call, failed-dial telephony spread over pickup."""
    connected = (_stt_cost(m) + _tts_cost(m) + _llm_cost(m)
                 + _telephony_connected(m) + _failed_dial_amortised(m))
    return round(connected, 2)


def breakdown(m: CallModel = DEFAULT_CALL) -> dict:
    return {
        "stt": round(_stt_cost(m), 3),
        "tts": round(_tts_cost(m), 3),
        "llm": round(_llm_cost(m), 3),
        "telephony_connected": round(_telephony_connected(m), 3),
        "failed_dial_amortised": round(_failed_dial_amortised(m), 3),
        "per_connected_call": voice_call_cost(m),
        "sources": "STT / TTS / LLM: SOURCED "
                   "(https://docs.sarvam.ai/api/getting-started/pricing). "
                   "telephony rates, call shape and speech density: ESTIMATED.",
    }


VOICE_CALL = voice_call_cost()


if __name__ == "__main__":
    import json
    print(json.dumps(breakdown(), indent=2))
