"""Audio clips on disk.

One MP3 per chat, plus one short intro clip per day. The web player plays them as
a playlist, which is what makes pause honest: there is no server-side concatenation
to get stuck inside, and re-rendering one chat after a failed synthesis costs one
request instead of the whole night.

The voice gateway streams with no Content-Length, so clips are written to a .part
file and renamed only once complete — a half-written clip is never served.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import config, speech, store, tts


def _slug(chat_id: str) -> str:
    return hashlib.sha1(chat_id.encode()).hexdigest()[:16]


def day_dir(day: str) -> Path:
    p = Path(config.AUDIO_DIR) / day
    p.mkdir(parents=True, exist_ok=True)
    return p


def chat_path(day: str, chat_id: str) -> Path:
    return day_dir(day) / f"{_slug(chat_id)}.mp3"


def intro_path(day: str) -> Path:
    return day_dir(day) / "intro.mp3"


def chat_url(day: str, chat_id: str, version: int | str | None = None) -> str:
    """URL for a clip. The ?v= is a cache-buster: browsers heuristically cache
    audio, and a clip rendered after a 404 would otherwise stay a 404 in the
    browser cache forever. Size is a stable, free version token."""
    url = f"/audio/{day}/{_slug(chat_id)}.mp3"
    return f"{url}?v={version}" if version else url


def intro_url(day: str, version: int | str | None = None) -> str:
    url = f"/audio/{day}/intro.mp3"
    return f"{url}?v={version}" if version else url


def seconds_of(mp3_bytes: int, speed: float = 1.0) -> float:
    """Duration from byte count. The gateway encodes CBR 128k (16 KB/s), which is
    accurate to well under a second for clips this short and needs no decoder."""
    if not mp3_bytes:
        return 0.0
    return round(mp3_bytes / 16000.0 * (speed or 1.0), 1)


def render_chat(day: str, row: dict, *, force: bool = False, speed: float | None = None) -> dict:
    """Synthesize one chat's segment. Returns {audio_path,url,audio_seconds,...}."""
    dest = chat_path(day, row["chat_id"])
    text = row.get("speech_text") or speech.chat_script(row)
    if not text:
        return {"audio_path": "", "audio_seconds": 0.0, "rendered": False, "reason": "nothing to say"}
    if dest.exists() and not force:
        n = dest.stat().st_size
        return {
            "audio_path": str(dest),
            "audio_bytes": n,
            "audio_seconds": seconds_of(n, speed if speed is not None else config.TTS_SPEED),
            "rendered": False,
            "cached": True,
        }
    n = tts.synthesize(text, dest, voice=config.TTS_VOICE, speed=speed if speed is not None else config.TTS_SPEED)
    return {
        "audio_path": str(dest),
        "audio_bytes": n,
        "audio_seconds": seconds_of(n, speed if speed is not None else config.TTS_SPEED),
        "rendered": True,
        "url": chat_url(day, row["chat_id"]),
        "chars": len(text),
    }


def render_intro(day: str, rows: list[dict], *, force: bool = False) -> dict:
    text = speech.briefing_script(day, rows)
    dest = intro_path(day)
    if dest.exists() and not force:
        return {"audio_path": str(dest), "audio_seconds": seconds_of(dest.stat().st_size), "rendered": False, "cached": True}
    n = tts.synthesize(text, dest, voice=config.TTS_VOICE)
    store.save_briefing(day, {
        "speech_text": text,
        "audio_path": str(dest),
        "audio_bytes": n,
        "audio_seconds": seconds_of(n),
        "chats": len(rows),
        "voice": config.TTS_VOICE,
    })
    return {"audio_path": str(dest), "audio_bytes": n, "audio_seconds": seconds_of(n), "rendered": True, "url": intro_url(day)}


def prune(keep_days: int) -> int:
    """Delete audio for days the UI no longer offers."""
    keep = set(store.days_available(keep_days))
    removed = 0
    root = Path(config.AUDIO_DIR)
    if not root.is_dir():
        return 0
    for d in root.iterdir():
        if d.is_dir() and d.name not in keep:
            for f in d.glob("*.mp3"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass
    return removed
