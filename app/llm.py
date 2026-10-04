"""The model that writes the roll-up.

ommlx, OpenAI-compatible, Qwen3.8-Flash-Next, thinking disabled (it otherwise
leaks chain-of-thought into `content`). Nothing leaves the tailnet: this is the
same endpoint sams-daily-news uses.

Replies are parsed from sentinel blocks rather than JSON because local models
mangle JSON structure far more often than they mangle two literal markers.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import config

SUMMARY_MARK = "===SUMMARY==="
DRAFT_MARK = "===DRAFT==="

SYSTEM = """You are Sam Townsend's chief of staff. You read one Signal conversation from the last 24 hours and write two things: a summary Sam can skim in ten seconds, and a reply he could send.

Hard rules:
- Plain prose. No markdown, no asterisks, no bullet characters, no headings, no emoji, no colons stacking up titles.
- Never invent a name, number, date, price, or event that is not in the transcript. If a detail is unclear, say so plainly instead of guessing.
- Name who said what. "Dana wants the deck by Friday" beats "someone wants something".
- Surface decisions, deadlines, dollar amounts, links, invites, and anything that needs an answer.
- Ignore spam, bot noise, and pure reaction traffic.
- Never mention that you are an AI. Never mention the summary is a summary."""

TASK = """Write two blocks, using exactly these two markers and nothing else.

{SUMMARY_MARK}
{summary_words} words or fewer. The most important thing first, then the rest. Write it so Sam can decide whether he needs to act without opening the app. If he was mentioned, the thing he was asked about comes first.

{DRAFT_MARK}
A reply Sam could send into this exact conversation, in his own voice: casual, brief, direct, contractions welcome, no sign-off, no "Hi" unless the conversation is cold. {draft_words} words or fewer. Address whoever asked him something. If the transcript genuinely needs nothing from him, write exactly NONE."""


def _request(messages: list[dict], *, max_tokens: int | None = None, temperature: float | None = None) -> str:
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
        "chat_template_kwargs": {"thinking": False, "enable_thinking": False},
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    key = config.llm_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(config.LLM_URL.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(), headers=headers, method="POST")
    last: Exception | None = None
    for attempt in range(config.LLM_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
                data = json.load(resp)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("no choices returned")
            msg = choices[0].get("message") or {}
            text = (msg.get("content") or "").strip()
            if not text:
                raise RuntimeError("empty completion")
            return text
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, TimeoutError, OSError) as exc:
            last = exc
            if attempt >= config.LLM_RETRIES:
                break
    raise RuntimeError(f"llm failed after {config.LLM_RETRIES + 1} attempts: {last}")


def _transcript(messages, cap: int = 9000) -> tuple[str, int]:
    """Timestamped transcript, newest kept when it has to be cut."""
    lines: list[str] = []
    for m in messages:
        who = "Sam" if m["is_self"] else (m["sender_name"] or "someone")
        stamp = f"[{m['clock']}] " if m.get("clock") else ""
        quote = f' (replying to: "{m["quote_text"][:90]}")' if m.get("quote_text") else ""
        media = f" [{m['media']}]" if m.get("media") else ""
        text = (m["text"] or "").strip()
        lines.append(f"{stamp}{who}: {text}{quote}{media}")
    out = "\n".join(lines)
    if len(out) > cap:
        out = "…earlier messages trimmed…\n" + out[-cap:]
    return out, len(lines)


def write_rollup(
    *,
    title: str,
    kind: str,
    participants: list[dict],
    messages,
    mentioned: bool,
    mention_labels: list[str],
    muted: bool = False,
) -> tuple[str, str, str]:
    """Returns (summary, draft, engine). engine is 'llm' or 'fallback'."""
    transcript, n = _transcript(messages)
    summary_words = config.SUMMARY_WORDS_GROUP if kind == "group" else config.SUMMARY_WORDS_DM

    header = [
        f"Conversation: {title or 'a Signal chat'}",
        f"Type: {'group chat' if kind == 'group' else 'direct message' if kind == 'dm' else 'note to self'}",
        f"Window: {n} messages in the last 24 hours.",
    ]
    if participants:
        header.append("Who wrote: " + ", ".join(f"{p['name']} ({p['count']})" for p in participants[:12]))
    if mentioned:
        who = ", ".join(mention_labels[:4]) if mention_labels else "by name"
        header.append(f"IMPORTANT: Sam was mentioned here ({who}). The draft must answer what he was asked.")
    else:
        header.append("Sam was not mentioned by name. A reply is optional; NONE is a good answer.")
    if muted:
        header.append("This chat is muted on his phone, so it is probably low priority.")

    user = "\n".join(header) + "\n\nTranscript, oldest first:\n" + transcript + "\n\n" + TASK.format(
        SUMMARY_MARK=SUMMARY_MARK, DRAFT_MARK=DRAFT_MARK,
        summary_words=summary_words, draft_words=config.DRAFT_WORDS_MAX,
    )
    messages_payload = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    try:
        raw = _request(messages_payload)
    except Exception:
        return mechanical_summary(messages), "", "fallback"

    summary, draft = split(raw)
    if not summary:
        summary = mechanical_summary(messages)
    return summary.strip(), draft.strip(), "llm"


def split(raw: str) -> tuple[str, str]:
    """Pull the two blocks out, tolerating a model that forgets a marker."""
    raw = raw.strip()
    summary, draft = "", ""
    if SUMMARY_MARK in raw and DRAFT_MARK in raw:
        head, rest = raw.split(SUMMARY_MARK, 1)
        summary, draft = rest.split(DRAFT_MARK, 1)
    elif DRAFT_MARK in raw:
        summary, draft = raw.split(DRAFT_MARK, 1)
    elif SUMMARY_MARK in raw:
        summary, draft = raw.split(SUMMARY_MARK, 1)
    else:
        parts = re.split(r"\n\s*(?:DRAFT|Reply)\s*:?", raw, maxsplit=1, flags=re.IGNORECASE)
        summary = parts[0]
        draft = parts[1] if len(parts) > 1 else ""
    summary = re.sub(r"^(?:summary|here['’]?s the summary|the summary)\s*:\s*", "", summary.strip(), flags=re.IGNORECASE)
    return summary.strip(), draft.strip()


def mechanical_summary(messages) -> str:
    """No-model fallback: still tells him what happened, cheaply."""
    if not messages:
        return ""
    senders: dict[str, int] = {}
    for m in messages:
        who = "Sam" if m["is_self"] else (m["sender_name"] or "someone")
        senders[who] = senders.get(who, 0) + 1
    tally = ", ".join(f"{n} ({c})" for n, c in sorted(senders.items(), key=lambda kv: -kv[1])[:5])
    last = [m for m in messages if (m["text"] or "").strip()][-3:]
    tail = " ".join((m["text"] or "").strip() for m in last)
    return f"{len(messages)} messages, from {tally}. Most recent: {tail[:280]}"
