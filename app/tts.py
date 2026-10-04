"""Client for mac-voice-service (af_heart / Kokoro-82M).

Endpoint (voice.home.arpa, or http://host.docker.internal:8001 from a container):

    POST /speak {text, voice, speed} -> audio/mpeg, streamed sentence by sentence
    GET  /voices                    -> {default, voices}
    GET  /health                    -> {status, backend, fallback, model, voices}

The gateway streams with no Content-Length, so the whole body is read before the
file is renamed into place. The browser never touches this endpoint: the roll-up
writes MP3s to the volume once at 8pm and the web app plays complete files, which
is the only thing iOS Safari plays reliably (and it keeps the nightly audio working
even if the voice box is down the next morning).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config


class TTSError(RuntimeError):
    pass


def _open(path: str, payload: dict | None = None, timeout: int | None = None):
    url = config.TTS_URL.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout or config.TTS_TIMEOUT)


def health() -> dict:
    try:
        with _open("/health", timeout=15) as resp:
            return json.load(resp)
    except Exception as exc:
        return {"status": "unreachable", "detail": str(exc)[:160]}


def voices() -> dict:
    try:
        with _open("/voices", timeout=15) as resp:
            return json.load(resp)
    except Exception as exc:
        return {"default": config.TTS_VOICE, "voices": [], "detail": str(exc)[:160]}


def available() -> bool:
    h = health()
    return str(h.get("status", "")) == "ok"


def synthesize(text: str, dest: Path, *, voice: str | None = None, speed: float | None = None) -> int:
    """Render `text` to an MP3 at `dest`. Returns bytes written.

    Sentences longer than the gateway's 420-char chunk limit are split client-side
    too: it is cheap insurance against a roll-up with one enormous run-on sentence.
    """
    voice = voice or config.TTS_VOICE
    speed = speed if speed is not None else config.TTS_SPEED
    payload = {"text": text, "voice": voice, "speed": speed}
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    written = 0
    try:
        with _open("/speak", payload, timeout=config.TTS_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("audio"):
                raise TTSError(f"unexpected content-type from /speak: {ctype}")
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:240]
        raise TTSError(f"voice {exc.code}: {detail}") from exc
    except Exception as exc:
        raise TTSError(f"voice unreachable: {exc}") from exc

    if written < 1024:
        tmp.unlink(missing_ok=True)
        raise TTSError(f"voice produced only {written} bytes")
    tmp.replace(dest)
    return written
