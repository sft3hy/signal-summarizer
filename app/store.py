"""SQLite persistence.

Two kinds of data live here and they have different lifetimes:

* `messages` is a **buffer**. The daemon streams messages continuously and the
  8pm job needs to read the last 24 hours, so the text has to exist somewhere
  for a while. It is deleted in the same run that writes the summaries, then
  VACUUMed, so the file on disk never accumulates a corpus of your chats.
* `rollups` is the product: summaries, drafted replies, mention flags, sender
  tallies. No message text.

Timestamps are epoch milliseconds (Signal's unit) everywhere; `day` is the local
date the roll-up belongs to, derived in config.TZ.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  chat_id      TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,              -- group | dm | self
  title        TEXT NOT NULL DEFAULT '',
  muted        INTEGER NOT NULL DEFAULT 0,
  members      INTEGER NOT NULL DEFAULT 0,
  first_ts     INTEGER NOT NULL DEFAULT 0,
  last_ts      INTEGER NOT NULL DEFAULT 0,
  last_read_ts INTEGER NOT NULL DEFAULT 0,
  hidden       INTEGER NOT NULL DEFAULT 0
);

-- BUFFER. Purged after every successful roll-up. Nothing references the text.
CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id      TEXT NOT NULL,
  ts           INTEGER NOT NULL,
  sender_aci   TEXT NOT NULL DEFAULT '',
  sender_name  TEXT NOT NULL DEFAULT '',
  text         TEXT NOT NULL DEFAULT '',
  is_self      INTEGER NOT NULL DEFAULT 0,
  is_reply     INTEGER NOT NULL DEFAULT 0,
  mention_kinds TEXT NOT NULL DEFAULT '',  -- csv: name,initial,nickname,mention-entity,reply
  quote_text   TEXT NOT NULL DEFAULT '',
  attachments  INTEGER NOT NULL DEFAULT 0,
  event_id     INTEGER,
  UNIQUE (chat_id, ts, sender_aci, is_self)
);
CREATE INDEX IF NOT EXISTS messages_window ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_id, ts);

CREATE TABLE IF NOT EXISTS rollups (
  day            TEXT NOT NULL,
  chat_id        TEXT NOT NULL,
  kind           TEXT NOT NULL,
  title          TEXT NOT NULL,
  summary        TEXT NOT NULL DEFAULT '',
  draft          TEXT NOT NULL DEFAULT '',
  needs_reply    INTEGER NOT NULL DEFAULT 0,
  mentioned      INTEGER NOT NULL DEFAULT 0,
  mention_labels TEXT NOT NULL DEFAULT '',
  msg_count      INTEGER NOT NULL DEFAULT 0,
  self_count     INTEGER NOT NULL DEFAULT 0,
  unread         INTEGER NOT NULL DEFAULT 0,   -- messages after your last read, counted at build time
  participants   TEXT NOT NULL DEFAULT '[]', -- JSON [{name,count}], no message text
  speech_text    TEXT NOT NULL DEFAULT '',   -- what af_heart reads: summary, not chats
  audio_path     TEXT NOT NULL DEFAULT '',
  audio_bytes    INTEGER NOT NULL DEFAULT 0,
  audio_seconds  REAL NOT NULL DEFAULT 0,
  first_ts       INTEGER NOT NULL DEFAULT 0,
  last_ts        INTEGER NOT NULL DEFAULT 0,
  model          TEXT NOT NULL DEFAULT '',
  engine         TEXT NOT NULL DEFAULT 'llm', -- llm | fallback
  created_at     INTEGER NOT NULL,
  PRIMARY KEY (day, chat_id)
);

-- The whole-day briefing: every chat read back to back in one MP3.
CREATE TABLE IF NOT EXISTS briefings (
  day           TEXT PRIMARY KEY,
  speech_text   TEXT NOT NULL DEFAULT '',
  audio_path    TEXT NOT NULL DEFAULT '',
  audio_bytes   INTEGER NOT NULL DEFAULT 0,
  audio_seconds REAL NOT NULL DEFAULT 0,
  chats         INTEGER NOT NULL DEFAULT 0,
  voice         TEXT NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  day         TEXT NOT NULL,
  started_at  INTEGER NOT NULL,
  finished_at INTEGER,
  status      TEXT NOT NULL,               -- running | ok | failed | busy
  window_from INTEGER NOT NULL DEFAULT 0,
  window_to   INTEGER NOT NULL DEFAULT 0,
  chats       INTEGER NOT NULL DEFAULT 0,
  messages    INTEGER NOT NULL DEFAULT 0,
  mentioned   INTEGER NOT NULL DEFAULT 0,
  purged      INTEGER NOT NULL DEFAULT 0,
  detail      TEXT NOT NULL DEFAULT '',
  error       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  kind   TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def local_zone():
    """The zoneinfo for config.TZ, falling back to a fixed PST offset."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.TZ)
    except Exception:  # pragma: no cover - tzdata missing in a slim image
        return timezone(timedelta(hours=-8))


def day_of(ts_ms: int | None = None) -> str:
    if not ts_ms:
        ts_ms = _utc_now_ms()
    return datetime.fromtimestamp(ts_ms / 1000, local_zone()).strftime("%Y-%m-%d")


def clock_of(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, local_zone()).strftime("%H:%M")


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def day_of_long(day: str | None = None) -> str:
    """'2026-10-03' -> 'Saturday, October 3rd'. Spoken, so no bare numbers."""
    if not day:
        day = day_of()
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(day or "")
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{_WEEKDAYS[d.weekday()]}, {_MONTHS[d.month - 1]} {n}{suffix}"


def window_start_ms(now_ms: int | None = None) -> tuple[int, int]:
    now_ms = now_ms or _utc_now_ms()
    return now_ms - config.WINDOW_HOURS * 3600 * 1000, now_ms


def next_run_ms(now_ms: int | None = None) -> int:
    """Epoch ms of the next RUN_AT in local time."""
    now_ms = now_ms or _utc_now_ms()
    now = datetime.now(local_zone())
    hh, mm = (int(x) for x in config.RUN_AT.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target.timestamp() * 1000 <= now_ms:
        target += timedelta(days=1)
    return int(target.timestamp() * 1000)


@contextmanager
def db():
    """One connection, committed on a clean exit, always closed."""
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Delete pages to zeros instead of leaving text in the freelist.
    conn.execute("PRAGMA secure_delete=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with db() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


# Columns added after the first cut. SQLite has no "add column if not exists",
# so compare against table_info and add what is missing. Keeps an existing
# volume usable across deploys instead of forcing a wipe.
_MIGRATIONS = {
    "rollups": {
        "speech_text": "TEXT NOT NULL DEFAULT ''",
        "audio_path": "TEXT NOT NULL DEFAULT ''",
        "audio_bytes": "INTEGER NOT NULL DEFAULT 0",
        "audio_seconds": "REAL NOT NULL DEFAULT 0",
        "unread": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, ddl in columns.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def set_meta(key: str, value: str | int | None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value if value is not None else "")),
        )


def get_meta(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def log_event(kind: str, detail: str = "") -> None:
    with db() as conn:
        conn.execute("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)", (_utc_now_ms(), kind, detail[:400]))
        conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 500 FROM events)")


def self_aci() -> str:
    return get_meta("self_aci")


def note_seen(ts_ms: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('last_seen_ts',?) "
            "ON CONFLICT(key) DO UPDATE SET value=CASE WHEN CAST(value AS INTEGER) < excluded.value THEN excluded.value ELSE value END",
            (str(int(ts_ms)),),
        )


# --- chats ---------------------------------------------------------------


def upsert_chat(chat_id: str, *, kind: str, title: str = "", muted: bool | None = None, members: int | None = None) -> None:
    with db() as conn:
        row = conn.execute("SELECT chat_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO chats(chat_id,kind,title,muted,members,first_ts,last_ts) VALUES(?,?,?,?,?,0,0)",
                (chat_id, kind, title, 1 if muted else 0, members or 0),
            )
        else:
            updates, vals = [], []
            if title:
                updates.append("title=?")
                vals.append(title)
            if kind:
                updates.append("kind=?")
                vals.append(kind)
            if muted is not None:
                updates.append("muted=?")
                vals.append(1 if muted else 0)
            if members:
                updates.append("members=?")
                vals.append(members)
            if updates:
                vals.append(chat_id)
                conn.execute(f"UPDATE chats SET {','.join(updates)} WHERE chat_id=?", vals)


def touch_chat(chat_id: str, ts_ms: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE chats SET last_ts=MAX(last_ts,?), first_ts=CASE WHEN first_ts=0 THEN ? ELSE first_ts END WHERE chat_id=?",
            (int(ts_ms), int(ts_ms), chat_id),
        )


def mark_read(chat_id: str, ts_ms: int | None = None) -> None:
    now = int(ts_ms or _utc_now_ms())
    with db() as conn:
        conn.execute("UPDATE chats SET last_read_ts=? WHERE chat_id=?", (now, chat_id))
        # The badge the app shows comes from rollups.unread, so clear it there too.
        conn.execute(
            "UPDATE rollups SET unread=0 WHERE chat_id=? AND last_ts<=?", (chat_id, now),
        )


# --- buffered messages --------------------------------------------------


def insert_message(
    *,
    chat_id: str,
    ts: int,
    sender_aci: str,
    sender_name: str,
    text: str,
    is_self: bool = False,
    is_reply: bool = False,
    mention_kinds: list[str] | None = None,
    quote_text: str = "",
    attachments: int = 0,
    event_id: int | None = None,
) -> bool:
    """Insert one buffered message. False means it was a duplicate."""
    with _LOCK, db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages(chat_id,ts,sender_aci,sender_name,text,is_self,is_reply,"
            "mention_kinds,quote_text,attachments,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                chat_id,
                int(ts),
                sender_aci,
                sender_name,
                text,
                1 if is_self else 0,
                1 if is_reply else 0,
                ",".join(mention_kinds) if isinstance(mention_kinds, (list, tuple, set))
                else (mention_kinds or ""),
                quote_text[:500],
                int(attachments),
                event_id,
            ),
        )
    return (cur.rowcount or 0) > 0


def chats_in_window(start_ms: int, end_ms: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            """
            SELECT c.chat_id, c.kind, c.title, c.muted, c.members, c.last_read_ts,
                   COUNT(m.id) AS msg_count,
                   SUM(m.is_self) AS self_count,
                   SUM(CASE WHEN m.mention_kinds != '' THEN 1 ELSE 0 END) AS mention_count,
                   MIN(m.ts) AS first_ts, MAX(m.ts) AS last_ts
              FROM messages m JOIN chats c ON c.chat_id = m.chat_id
             WHERE m.ts >= ? AND m.ts <= ?
             GROUP BY c.chat_id
             ORDER BY mention_count DESC, msg_count DESC, last_ts DESC
            """,
            (int(start_ms), int(end_ms)),
        ).fetchall()


def unread_count(chat_id: str, start_ms: int, end_ms: int, last_read_ts: int) -> int:
    """Messages from other people after the last time you opened this chat.

    Counted here, at build time, because this is the last moment the raw rows
    still exist. mark_read() zeroes the stored number afterwards.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE chat_id=? AND ts>=? AND ts<=? AND is_self=0 AND ts>?",
            (chat_id, int(start_ms), int(end_ms), int(last_read_ts or 0)),
        ).fetchone()
    return int(row["n"] or 0)


def messages_for(chat_id: str, start_ms: int, end_ms: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND ts>=? AND ts<=? ORDER BY ts ASC",
            (chat_id, int(start_ms), int(end_ms)),
        ).fetchall()


def buffer_stats() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest FROM messages"
        ).fetchone()
        size = Path(config.DB_PATH).stat().st_size if Path(config.DB_PATH).exists() else 0
    return {
        "buffered_messages": row["n"] or 0,
        "oldest_ts": row["oldest"],
        "newest_ts": row["newest"],
        "db_bytes": size,
        "purge_policy": "wipe-after-rollup" if config.PURGE_RAW_AFTER_ROLLUP else "retained",
        "secure_delete": True,
    }


def purge_buffer(before_ms: int | None = None, *, vacuum: bool = True) -> int:
    """Delete buffered message text.

    Default policy (SS_KEEP_RAW unset): the buffer is emptied completely, because
    the roll-up has already turned it into a summary. With SS_KEEP_RAW=1 the
    caller passes a horizon instead and only older text goes.

    VACUUM runs on its own autocommit connection: SQLite refuses it inside a
    transaction, and the failure used to roll the DELETE back with it — leaving
    every raw message on disk while reporting a clean wipe.
    """
    with _LOCK, db() as conn:
        if before_ms is None:
            cur = conn.execute("DELETE FROM messages")
        else:
            cur = conn.execute("DELETE FROM messages WHERE ts <= ?", (int(before_ms),))
        deleted = cur.rowcount or 0
    if deleted and vacuum:
        _vacuum()
    return deleted


def purge_window(start_ms: int, end_ms: int, *, vacuum: bool = True) -> int:
    """Delete only the buffered messages inside one window.

    A roll-up must never destroy text it did not summarize. The nightly default
    (end = now, 24h window) clears the whole live buffer, so the privacy promise
    holds; a `--hours 6` or `--day` run then only wipes what it actually covered,
    and the older tail survives to be summarized by the real 8pm job.
    """
    with _LOCK, db() as conn:
        cur = conn.execute("DELETE FROM messages WHERE ts>=? AND ts<=?",
                           (int(start_ms), int(end_ms)))
        deleted = cur.rowcount or 0
    if deleted and vacuum:
        _vacuum()
    return deleted


def _vacuum() -> None:
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.isolation_level = None          # autocommit, required by VACUUM
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log_event("vacuum-failed", str(exc)[:160])


# --- roll-ups -----------------------------------------------------------


def save_rollup(day: str, row: dict) -> None:
    row = {
        **{
            "day": day, "chat_id": "", "kind": "dm", "title": "", "summary": "", "draft": "",
            "needs_reply": 0, "mentioned": 0, "mention_labels": "", "msg_count": 0,
            "self_count": 0, "participants": "[]", "speech_text": "", "audio_path": "",
            "audio_bytes": 0, "audio_seconds": 0.0, "first_ts": 0, "last_ts": 0, "unread": 0,
            "model": "", "engine": "llm", "created_at": _utc_now_ms(),
        },
        **row,
    }
    # rollups_for() hands back participants and mention_labels already decoded
    # (list and CSV-split). Any caller that round-trips a read row back through
    # here — /api/say does — would otherwise bind a Python list and SQLite would
    # raise ProgrammingError. Normalise the storage shape at the boundary.
    if isinstance(row["participants"], (list, tuple, dict)):
        row["participants"] = json.dumps(row["participants"])
    if isinstance(row["mention_labels"], (list, tuple)):
        row["mention_labels"] = ",".join(str(x) for x in row["mention_labels"])
    with db() as conn:
        conn.execute(
            """
            INSERT INTO rollups(day,chat_id,kind,title,summary,draft,needs_reply,mentioned,
                                mention_labels,msg_count,self_count,unread,participants,speech_text,
                                audio_path,audio_bytes,audio_seconds,first_ts,last_ts,model,engine,created_at)
            VALUES(:day,:chat_id,:kind,:title,:summary,:draft,:needs_reply,:mentioned,
                  :mention_labels,:msg_count,:self_count,:unread,:participants,:speech_text,
                  :audio_path,:audio_bytes,:audio_seconds,:first_ts,:last_ts,:model,:engine,:created_at)
            ON CONFLICT(day,chat_id) DO UPDATE SET
              summary=excluded.summary, draft=excluded.draft, needs_reply=excluded.needs_reply,
              mentioned=excluded.mentioned, mention_labels=excluded.mention_labels,
              msg_count=excluded.msg_count, self_count=excluded.self_count,
              unread=excluded.unread,
              participants=excluded.participants, speech_text=excluded.speech_text,
              audio_path=excluded.audio_path, audio_bytes=excluded.audio_bytes,
              audio_seconds=excluded.audio_seconds, first_ts=excluded.first_ts,
              last_ts=excluded.last_ts, model=excluded.model, engine=excluded.engine,
              created_at=excluded.created_at
            """,
            row,
        )


def save_briefing(day: str, row: dict) -> None:
    row = {
        **{"speech_text": "", "audio_path": "", "audio_bytes": 0, "audio_seconds": 0.0,
           "chats": 0, "voice": "", "created_at": _utc_now_ms()},
        **row,
    }
    row["day"] = day
    with db() as conn:
        conn.execute(
            """
            INSERT INTO briefings(day,speech_text,audio_path,audio_bytes,audio_seconds,chats,voice,created_at)
            VALUES(:day,:speech_text,:audio_path,:audio_bytes,:audio_seconds,:chats,:voice,:created_at)
            ON CONFLICT(day) DO UPDATE SET
              speech_text=excluded.speech_text, audio_path=excluded.audio_path,
              audio_bytes=excluded.audio_bytes, audio_seconds=excluded.audio_seconds,
              chats=excluded.chats, voice=excluded.voice, created_at=excluded.created_at
            """,
            row,
        )


def get_briefing(day: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM briefings WHERE day=?", (day,)).fetchone()
    return dict(row) if row else None


def rollups_for(day: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT r.*, c.last_read_ts, c.muted
              FROM rollups r LEFT JOIN chats c ON c.chat_id = r.chat_id
             WHERE r.day=?
             ORDER BY r.mentioned DESC, r.msg_count DESC, r.last_ts DESC
            """,
            (day,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["participants"] = json.loads(d.get("participants") or "[]")
        d["mention_labels"] = [x for x in (d.get("mention_labels") or "").split(",") if x]
        # `unread` is counted when the roll-up is built (messages after your last
        # read) and zeroed by mark_read. It is not recomputed later, because the
        # raw rows that made it up are already gone by design.
        d["unread"] = int(d.get("unread") or 0)
        out.append(d)
    return out


def days_available(limit: int | None = None) -> list[str]:
    limit = limit or config.DAYS_SHOWN
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT day FROM rollups ORDER BY day DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [r["day"] for r in rows]


# --- runs --------------------------------------------------------------


def start_run(day: str, window: tuple[int, int]) -> int | None:
    """Insert a `running` row, or None if one is already in flight (lock-free
    mutual exclusion via the unique partial index below)."""
    with db() as conn:
        busy = conn.execute("SELECT id FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        if busy:
            started = int(busy["started_at"])
            if _utc_now_ms() - started < config.BUILD_LOCK_STALE_SECONDS * 1000:
                return None
            conn.execute(
                "UPDATE runs SET status='failed', error='stale lock reclaimed', finished_at=? WHERE id=?",
                (_utc_now_ms(), busy["id"]),
            )
        cur = conn.execute(
            "INSERT INTO runs(day,started_at,status,window_from,window_to) VALUES(?,?,?,?,?)",
            (day, _utc_now_ms(), "running", int(window[0]), int(window[1])),
        )
    return cur.lastrowid


def finish_run(run_id: int, status: str, *, chats: int = 0, messages: int = 0, mentioned: int = 0, purged: int = 0, detail: str = "", error: str = "") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, chats=?, messages=?, mentioned=?, purged=?, detail=?, error=? WHERE id=?",
            (status, _utc_now_ms(), int(chats), int(messages), int(mentioned), int(purged), detail[:400], error[:400], run_id),
        )


def last_run() -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def recent_events(limit: int = 40) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]
