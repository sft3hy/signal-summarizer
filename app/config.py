"""Signal roll-up configuration.

Stdlib only. Every value reads the environment first, so the container never
needs a code edit; the defaults are the values this Studio actually runs.

Time handling matters here: the roll-up is due at 20:00 *local* time, so every
timestamp is stored in epoch milliseconds and interpreted in TZ (default
America/Los_Angeles). Nothing is stored as a naive local string.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    """Env first, default otherwise. An unset var must fall back to the default,
    not to an empty string — that silently turned float knobs into ValueError."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# --- Paths -------------------------------------------------------------------

# The container mounts a volume at /data. On the host (tests, scripts) there is
# no /data, so fall back to ~/.signal-summarizer instead of writing to root.
_default_data = "/data" if Path("/data").is_dir() else str(Path.home() / ".signal-summarizer")
DATA_DIR = Path(_env("SS_DATA_DIR", _default_data))
DB_PATH = Path(_env("SS_DB_PATH", str(DATA_DIR / "rollup.sqlite3")))
LOG_DIR = DATA_DIR / "logs"

# --- signal-cli daemon -----------------------------------------------------

# Compose puts the daemon on an internal-only bridge network; it publishes no
# ports to the host at all. The summarizer is the only thing that can reach it.
SIGNAL_RPC_URL = _env("SS_SIGNAL_RPC_URL", "http://signal-cli:8080")
DEVICE_NAME = _env("SS_DEVICE_NAME", "signal-summarizer")

# How long to keep the SSE stream's last event id so a restart resumes where it
# stopped. signal-cli keeps the last 1000 events in memory.
SSE_RECONNECT_MIN_WAIT = 2
SSE_RECONNECT_MAX_WAIT = 60

# --- Web app ---------------------------------------------------------------

WEB_HOST = _env("SS_WEB_HOST", "0.0.0.0")
WEB_PORT = _int("SS_WEB_PORT", 8080)
AUTH_USER = _env("SS_AUTH_USER", "signal")
AUTH_PASSWORD = _env("SS_AUTH_PASSWORD", "")

# Refuse to serve plaintext chat summaries to the whole tailnet by accident.
ALLOW_UNAUTHENTICATED = _bool("SS_ALLOW_UNAUTHENTICATED", False)

# How many past days the picker offers.
DAYS_SHOWN = _int("SS_DAYS_SHOWN", 14)

# --- LLM -----------------------------------------------------------------

# omlx binds the tailnet IP only (see edge/README), so this is reachable from a
# container on the edge network but not from the host loopback.
LLM_URL = _env("SS_LLM_URL", "http://100.122.197.81:8000/v1")
LLM_MODEL = _env("SS_LLM_MODEL", "Qwen3.8-Flash-Next-oQ4e-mtp")
LLM_TIMEOUT = _int("SS_LLM_TIMEOUT", 180)
LLM_TEMPERATURE = _float("SS_LLM_TEMPERATURE", 0.4)
LLM_MAX_TOKENS = _int("SS_LLM_MAX_TOKENS", 900)
LLM_RETRIES = _int("SS_LLM_RETRIES", 2)
# Target length for one chat's summary, in words. Groups get more room than DMs.
SUMMARY_WORDS_GROUP = _int("SS_SUMMARY_WORDS_GROUP", 110)
SUMMARY_WORDS_DM = _int("SS_SUMMARY_WORDS_DM", 55)
DRAFT_WORDS_MAX = _int("SS_DRAFT_WORDS_MAX", 45)


def llm_api_key() -> str:
    """Bearer key for omlx.

    The container gets SS_LLM_API_KEY from .env. On the host, fall back to the
    keys already exported in ~/.zshrc (same approach as sams-daily-news) so
    scripts/check.py works without re-typing a secret.
    """
    for name in ("SS_LLM_API_KEY", "OMLX_API_KEY", "OPENAI_API_KEY"):
        key = _env(name)
        if key:
            return key
    zshrc = Path.home() / ".zshrc"
    if zshrc.is_file():
        try:
            text = zshrc.read_text()
        except OSError:
            return ""
        for name in ("SS_LLM_API_KEY", "OMLX_API_KEY", "OPENAI_API_KEY"):
            m = re.search(rf"export\s+{name}=[\"']?([^\"'\n]+)", text)
            if m:
                return m.group(1).strip()
    return ""


# --- Roll-up schedule ----------------------------------------------------

TZ = _env("TZ", "America/Los_Angeles")
WINDOW_HOURS = _int("SS_WINDOW_HOURS", 24)
# The cron line. Also what the UI prints as "next roll-up".
RUN_AT = _env("SS_RUN_AT", "20:00")
# Belt and braces: if the container starts after RUN_AT and today's roll-up
# never happened (restart, slept-through cron), run it on boot.
CATCH_UP_ON_BOOT = _bool("SS_CATCH_UP_ON_BOOT", True)
BUILD_LOCK_STALE_SECONDS = _int("SS_BUILD_LOCK_STALE", 1800)

# --- Voice (mac-voice-service) -------------------------------------------

# Reachable from the edge network as voice.home.arpa, or directly on the host's
# published port. Inside the container host.docker.internal is the short path.
TTS_URL = _env("SS_TTS_URL", "http://host.docker.internal:8001")
TTS_VOICE = _env("SS_TTS_VOICE", "af_heart")
TTS_SPEED = _float("SS_TTS_SPEED", 1.0)
TTS_TIMEOUT = _int("SS_TTS_TIMEOUT", 900)
TTS_ENABLED = not _bool("SS_NO_TTS", False)
AUDIO_DIR = DATA_DIR / "audio"
# Seconds of silence inserted between chats in the whole-day briefing.
AUDIO_GAP_SECONDS = _int("SS_AUDIO_GAP_SECONDS", 1)


# --- Privacy -------------------------------------------------------------

# "Summaries + counts only": raw text is buffered so the 8pm job has something
# to read, then wiped in the same transaction that stores the summaries. The
# buffer therefore never holds more than the current window.
PURGE_RAW_AFTER_ROLLUP = not _bool("SS_KEEP_RAW", False)
# Envelope structure is dumped (keys only, never message text) to help adapt the
# parser to a new signal-cli JSON shape.
LOG_ENVELOPE_SCHEMA = _bool("SS_LOG_ENVELOPE_SCHEMA", True)

# --- Mentions ----------------------------------------------------------

# Full names, matched case-insensitively with word boundaries. These are the
# only names that flag a mention on their own.
FULL_NAMES = [
    s.strip()
    for s in _env("SS_MENTION_FULL_NAMES", "Sam Townsend,Samuel Townsend").split(",")
    if s.strip()
]
# First name + surname initial, as people actually type it: "Sam T", "sam t.".
FIRST_NAMES = [
    s.strip() for s in _env("SS_MENTION_FIRSTS", "Sam,Samuel,Samual,Samuell,Sammy").split(",")
    if s.strip()
]
# The surname initial used by that form.
MENTION_INITIAL = _env("SS_MENTION_INITIAL", "T")
# Nicknames flag a mention alone, no surname needed.
NICKNAMES = [s.strip() for s in _env("SS_MENTION_NICKNAMES", "Slammy,Slammie").split(",") if s.strip()]
# A reply that quote-references one of his messages counts as a mention.
COUNT_REPLIES = _bool("SS_MENTION_REPLIES", True)
# Deliberately NOT matched: a bare "Sam". There are other Sams.
