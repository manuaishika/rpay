"""sarvam.py -- a tiny stdlib client for the Sarvam AI APIs (chat + TTS).

Optional. Nothing else in the package imports this at module load; if there
is no API key the rest of `recovery/` runs exactly as before. Used only by
recovery/voice.py to turn a decided voice call into an actual Hinglish
audio file you can listen to.

Key resolution order:
    1. env var SARVAM_API_KEY
    2. recovery/.env   (KEY=VALUE lines, gitignored)
    3. ./.env
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"
TTS_URL = "https://api.sarvam.ai/text-to-speech"

CHAT_MODEL = "sarvam-105b-conversations"
TTS_MODEL = "bulbul:v3"
TTS_CHAR_LIMIT = 2500                # bulbul:v3

_ENV_FILES = (Path(__file__).with_name(".env"), Path(".env"))


class SarvamError(RuntimeError):
    pass


def _read_env_file(path: Path) -> dict:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_key() -> str | None:
    key = os.environ.get("SARVAM_API_KEY")
    if key:
        return key.strip()
    for path in _ENV_FILES:
        val = _read_env_file(path).get("SARVAM_API_KEY")
        if val:
            return val
    return None


def available() -> bool:
    return load_key() is not None


class Sarvam:
    def __init__(self, key: str | None = None, timeout: float = 60.0):
        self.key = key or load_key()
        if not self.key:
            raise SarvamError(
                "no Sarvam API key. Set SARVAM_API_KEY or put it in recovery/.env"
            )
        self.timeout = timeout

    # -- low-level ------------------------------------------------------
    def _post(self, url: str, payload: dict, retries: int = 2) -> dict:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "api-subscription-key": self.key,
            "Authorization": f"Bearer {self.key}",
        }
        last = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                last = SarvamError(f"HTTP {e.code} from {url}: {detail}")
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last
            except urllib.error.URLError as e:
                last = SarvamError(f"network error calling {url}: {e.reason}")
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last
        raise last  # pragma: no cover

    # -- chat --------------------------------------------------------
    def chat(self, messages: list[dict], model: str = CHAT_MODEL,
             temperature: float = 0.3, max_tokens: int = 500,
             reasoning_effort: str | None = None) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        data = self._post(CHAT_URL, payload)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise SarvamError(f"unexpected chat response: {json.dumps(data)[:400]}") from e
        # sarvam-105b is a reasoning model: the answer is in `content`, but a
        # token-starved response can leave content null with text in reasoning.
        text = msg.get("content") or msg.get("reasoning_content")
        if not text:
            raise SarvamError(f"empty chat content: {json.dumps(data)[:400]}")
        return text.strip()

    # -- text to speech --------------------------------------------------
    def text_to_speech(self, text: str, language_code: str = "hi-IN",
                       speaker: str = "priya", model: str = TTS_MODEL,
                       sample_rate: int = 22050) -> bytes:
        if len(text) > TTS_CHAR_LIMIT:
            raise SarvamError(f"text is {len(text)} chars, over the {TTS_CHAR_LIMIT} limit")
        data = self._post(TTS_URL, {
            "text": text,
            "language_code": language_code,
            "speaker": speaker,
            "model": model,
            "speech_sample_rate": sample_rate,
        })
        audios = data.get("audios") or []
        if not audios:
            raise SarvamError(f"no audio in TTS response: {json.dumps(data)[:400]}")
        return b"".join(base64.b64decode(a) for a in audios)
