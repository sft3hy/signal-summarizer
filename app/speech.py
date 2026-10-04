"""Spoken scripts for af_heart.

The web player plays one MP3 per chat (see audio.py) rather than one stitched
file, so pause/seek stay exact per chat and a chat that fails to synthesize does
not spoil the whole night's briefing.

Text here is written for the ear. Kokoro reads digits fine, but markup, emoji and
URLs read terribly, so everything is normalised into sentences first.
"""

from __future__ import annotations

import re

from . import store

_ORDS = {
    "&": " and ",
    "@": " at ",
    "%": " percent ",
    "etc.": "et cetera",
    "e.g.": "for example",
    "i.e.": "that is",
    "vs.": "versus",
    "Mr.": "Mister",
    "Mrs.": "Missus",
    "Dr.": "Doctor",
    "St.": "Street",
    "OK": "okay",
    "okay": "okay",
}

_EMojis = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200d]"
)


def for_ear(text: str) -> str:
    """Plain, speakable prose. No markdown, no emoji, no URLs, no @handles."""
    if not text:
        return ""
    out = text
    out = re.sub(r"https?://\S+", "a link", out)
    out = re.sub(r"<[^>]+>", " ", out)
    out = _EMojis.sub(" ", out)
    out = re.sub(r"[*_#`~>|]", " ", out)
    out = re.sub(r"@([A-Za-z0-9_.\-]+)", r"\1", out)
    for k, v in _ORDS.items():
        out = out.replace(k, v)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{2,}", ". ", out)
    out = out.replace("\n", " ")
    # A sentence needs punctuation at the end or Kokoro runs tracks together.
    out = out.strip()
    if out and out[-1] not in ".!?":
        out += "."
    return out


def _n(n: int, singular: str, plural: str | None = None) -> str:
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


def chat_intro(row: dict) -> str:
    """One-sentence header spoken before each chat's summary."""
    kind = {"group": "group chat", "dm": "message thread", "self": "note to yourself"}.get(row.get("kind", ""), "chat")
    title = (row.get("title") or "Untitled").strip()
    bits = [f"{title}, a {kind}"]
    others = max(0, int(row.get("msg_count", 0)) - int(row.get("self_count", 0)))
    bits.append(f"{_n(others, 'message')} from {len(row.get('participants') or [1])} people" if len(row.get("participants") or []) > 1 else f"{_n(others, 'message')}")
    if row.get("mentioned"):
        labels = row.get("mention_labels") or []
        who = f", including {', '.join(labels[:3])}" if labels else ""
        bits.append(f"you were mentioned {int(row['mentioned'])} time{'s' if int(row['mentioned']) != 1 else ''}{who}")
    if row.get("muted"):
        bits.append("this chat is muted")
    return "; ".join(bits) + "."


def chat_script(row: dict) -> str:
    """The whole spoken segment for one chat: intro, summary, draft."""
    parts = [for_ear(chat_intro(row))]
    summary = for_ear(row.get("summary", ""))
    if summary:
        parts.append(summary)
    draft = for_ear(row.get("draft", ""))
    if draft and not row.get("no_reply"):
        parts.append(f"Draft reply for you to send. {draft}")
    elif row.get("no_reply"):
        parts.append("Nothing in there needs a reply from you.")
    return " ".join(p for p in parts if p)


def briefing_script(day: str, rows: list[dict]) -> str:
    """Opening line for the day, used in the player and stored for re-renders."""
    human = store.day_of_long(day)
    mentioned = sum(1 for r in rows if r.get("mentioned"))
    messages = sum(int(r.get("msg_count", 0)) for r in rows)
    lead = f"Your Signal roll-up for {human}. {len(rows)} chats, {_n(messages, 'message')}"
    if mentioned:
        lead += f", and you were mentioned in {mentioned} of them"
    lead += "."
    return lead
