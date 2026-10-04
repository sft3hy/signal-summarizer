"""Mention detection.

The one promise that must never break: only *him*. A bare "Sam" never flags, and
neither does a Sam with any other surname.

Three tiers, any of which flags a message:

1. full name     his first name (including listed misspellings) + his surname,
                 with an optional middle initial and any spacing between them
2. initial form  "Sam T", "sam t.", "Samuel T-" — the initial has to be a token
                 on its own, which is what keeps "Sam the barista", "sam
                 technology" and "Sam Tang" out
3. entities      signal-cli 0.14.8 reports mentions as {start,length,name,uuid}.
                 Matching on uuid is authoritative even when the rendered text is
                 odd, and a reply that quotes one of his messages counts too.

Every pattern is wrapped in `(?<![\\w@])` … `(?![\\w])`, so "Samir Townsend" and
"slammytown" cannot match by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import config

_SP = r"[\s.\u00a0\-]+"          # anything people type between name parts
_BOUND_L = r"(?<![\w@])"
_BOUND_R = r"(?![\w])"

# Explicit surname typos rather than a clever fuzzy matcher: a name is a small,
# closed set of mistakes, and an explicit list can never fire on a stranger.
_SURNAME_TYPOS = {
    "townsend": r"townsend|towsend|townsen|townsed|tounsend|townsened|townsande|townsendt",
}


def _alternatives(words: list[str]) -> str:
    """Longest first, so 'Samuel' wins before 'Sam' can eat part of it."""
    uniq = sorted({w for w in words if w}, key=len, reverse=True)
    return "|".join(re.escape(w) for w in uniq)


def _surname_pattern(word: str) -> str:
    key = word.lower()
    if key in _SURNAME_TYPOS:
        return _SURNAME_TYPOS[key]
    return re.escape(word)


def _last_token(name: str) -> str:
    return name.split()[-1]


def _first_alternation() -> str:
    """His first names and misspellings. Never matched on their own."""
    return _alternatives(list(config.FULL_NAMES and [n.split()[0] for n in config.FULL_NAMES]) + list(config.FIRST_NAMES))


def _surname_alternation() -> str:
    surnames = [_last_token(n) for n in config.FULL_NAMES if len(n.split()) > 1]
    return "|".join(_surname_pattern(s) for s in sorted(set(surnames), key=len, reverse=True)) or r"(?!)"


def full_name_pattern() -> re.Pattern[str]:
    """'Sam Townsend', 'Samuel J. Townsend', 'Samual Townsen', 'Sam-Townsend'."""
    if not config.FULL_NAMES:
        return re.compile(r"(?!)")
    middle = rf"(?:{_SP}[A-Z](?={_SP}))?"
    return re.compile(
        rf"{_BOUND_L}(?:{_first_alternation()}){middle}{_SP}(?:{_surname_alternation()}){_BOUND_R}",
        re.IGNORECASE,
    )


def initial_pattern() -> re.Pattern[str]:
    """'Sam T' and friends. The trailing guard allows punctuation, not letters."""
    initial = config.MENTION_INITIAL or "T"
    if not config.FIRST_NAMES:
        return re.compile(r"(?!)")
    return re.compile(
        rf"{_BOUND_L}(?:{_first_alternation()}){_SP}{re.escape(initial)}{_BOUND_R}",
        re.IGNORECASE,
    )


def nickname_pattern() -> re.Pattern[str]:
    if not config.NICKNAMES:
        return re.compile(r"(?!)")
    return re.compile(rf"{_BOUND_L}(?:{_alternatives(config.NICKNAMES)}){_BOUND_R}", re.IGNORECASE)


FULL_RE = full_name_pattern()
INITIAL_RE = initial_pattern()
NICKNAME_RE = nickname_pattern()


@dataclass
class Mention:
    """One reason a message feels aimed at him."""

    kind: str  # name | initial | nickname | at-mention | reply
    detail: str

    def label(self) -> str:
        return self.detail


def _strip_blockquotes(text: str) -> str:
    """Drop '>' lines so a quoted copy of an old message does not re-flag it."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def scan_text(text: str) -> list[Mention]:
    """Pattern hits for one message body, at most one per tier."""
    if not text:
        return []
    body = _strip_blockquotes(text)
    hits: list[Mention] = []

    m = FULL_RE.search(body)
    if m:
        hits.append(Mention("name", m.group(0)))
    m = NICKNAME_RE.search(body)
    if m:
        hits.append(Mention("nickname", m.group(0)))
    if not hits:
        m = INITIAL_RE.search(body)
        if m:
            hits.append(Mention("initial", m.group(0)))
    return hits


def scan_envelope(
    text: str,
    *,
    mention_entities: list[dict] | None = None,
    quote_author_aci: str | None = None,
    self_aci: str | None = None,
    self_ids: list[str] | None = None,
) -> list[Mention]:
    """Every mention signal on one message.

    mention_entities is dataMessage.mentions: [{start,length,name,uuid}, ...].
    """
    def is_mine(value: str | None) -> bool:
        if not value:
            return False
        probe = str(value).strip().lower()
        ids = {(self_aci or "").lower()} | {(i or "").lower() for i in (self_ids or [])}
        ids.discard("")
        if probe in ids:
            return True
        digits = "".join(c for c in probe if c.isdigit())
        if len(digits) >= 10:
            return any(digits == "".join(c for c in c2 if c.isdigit()) for c2 in ids)
        return False

    hits = scan_text(text)

    for ent in mention_entities or []:
        if not isinstance(ent, dict):
            continue
        uuid = str(ent.get("uuid") or ent.get("aci") or "")
        name = str(ent.get("name") or "").strip()
        covered = ""
        try:
            covered = text[int(ent.get("start", 0)) : int(ent.get("start", 0)) + int(ent.get("length", 0))]
        except (TypeError, ValueError):
            pass
        if is_mine(uuid):
            hits.append(Mention("at-mention", (name or covered or "@you").lstrip("@")))
            continue
        # No usable uuid (older relay, or the daemon dropped it): fall back to
        # the covered text, which still has to match a full-name pattern.
        # Signal renders the handle with a leading "@", which our own lookbehind
        # guard would otherwise reject. It is markup, not part of the name.
        probe = (name or covered).strip().lstrip("@").strip()
        if probe and (FULL_RE.search(probe) or FULL_RE.fullmatch(probe)):
            hits.append(Mention("at-mention", probe))

    if config.COUNT_REPLIES and is_mine(quote_author_aci):
        hits.append(Mention("reply", "replied to you"))

    return hits


def kinds(hits: list[Mention]) -> list[str]:
    out: list[str] = []
    for h in hits:
        if h.kind not in out:
            out.append(h.kind)
    return out


def labels(hits_by_message: list[list[Mention]], limit: int = 4) -> list[str]:
    """Human labels for a whole window, de-duplicated, oldest order kept."""
    seen: list[str] = []
    for hits in hits_by_message:
        for h in hits:
            text = h.detail or {"reply": "replied to you", "at-mention": "@mentioned"}.get(h.kind, "")
            if text and text not in seen:
                seen.append(text)
    return seen[:limit]
