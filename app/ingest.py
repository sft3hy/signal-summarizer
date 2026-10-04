"""Envelope ingestion: SSE -> buffered rows.

Field names follow the schemas signal-cli 0.14.8 publishes
(signal-cli-0.14.8-json-schemas.tar.gz), which are camelCase:

    envelope   {source, sourceUuid, sourceName, sourceNumber, sourceDevice,
                timestamp, serverReceivedTimestamp, dataMessage, syncMessage,
                receiptMessage, callMessage, storyMessage, typingMessage, editMessage}
    dataMessage{message, mentions[], groupInfo{groupId,groupName}, quote{authorUuid,...},
                attachments[], reaction{emoji,targetAuthorUuid}, preview[], ...}
    syncMessage{sentMessage{destinationUuid,groupInfo,...}, readMessages[{senderUuid,...}]}

snake_case fallbacks are kept because the pre-0.13 wire format used it and a
rollback should degrade, not crash.
"""

from __future__ import annotations

import base64
import binascii
import re
import threading
import time
import traceback
from typing import Any

from . import config, mentions, signalrpc, store


def first(d: dict, *keys: str, default: Any = None) -> Any:
    """First present key — camelCase first, snake_case as the fallback."""
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def canonical_group_id(raw: str) -> str:
    """Base64 group ids arrive padded, unpadded, or url-safe. One shape only."""
    if not raw:
        return ""
    original = str(raw)
    s = original.strip().replace("-", "+").replace("_", "/")
    if not s or re.search(r"[^A-Za-z0-9+/=]", s):
        return original            # not base64 at all; keep it verbatim
    try:
        raw_bytes = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (binascii.Error, ValueError):
        return original
    if not raw_bytes:
        return original
    return base64.b64encode(raw_bytes).decode()


def voice_note_of(attachments: list[dict] | None) -> bool:
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        if first(a, "isVoiceNote", "voice_note") is True:
            return True
        ctype = str(first(a, "contentType", "content_type", default=""))
        if ctype.startswith("audio/"):
            return True
        name = str(first(a, "filename", default="")).lower()
        if name.endswith((".m4a", ".mp3", ".ogg", ".aac", ".opus", ".wav", ".caf", ".mp4")):
            return True
    return False


def attachment_kind(attachments: list[dict] | None) -> tuple[str, int]:
    """(label, count) for a media description. Types only, never files."""
    items = [a for a in (attachments or []) if isinstance(a, dict)]
    if not items:
        return "", 0
    n = len(items)
    if voice_note_of(items):
        return f"{n} voice note{'s' if n != 1 else ''}", n
    ctypes = {str(first(a, "contentType", "content_type", default="")) for a in items}
    if ctypes and all(c.startswith("video/") for c in ctypes):
        return f"{n} video{'s' if n != 1 else ''}", n
    if ctypes and all(c.startswith("image/") for c in ctypes):
        return f"{n} photo{'s' if n != 1 else ''}", n
    if any(first(a, "filename", default="") for a in items):
        names = [str(first(a, "filename", default="")) for a in items if first(a, "filename", default="")]
        label = f"{n} file{'s' if n != 1 else ''} ({', '.join(names[:3])})"
        return label, n
    return f"{n} attachment{'s' if n != 1 else ''}", n


class Ingest:
    """Owns the SSE reader thread and the account/chat registries."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.names: dict[str, str] = {}          # aci -> display name
        self.groups: dict[str, str] = {}          # groupId -> title
        self.self_aci: str = ""
        self.self_number: str = ""
        self.account: str = ""          # what the daemon wants as `account`
        self.linked = False
        self.connected = False
        self.last_event_id: str | None = None
        self.last_event_ts: int = 0
        self.errors: int = 0
        self.envelope_kinds: dict[str, int] = {}
        self._last_registry_refresh: int = 0

    # --- account ------------------------------------------------------

    def refresh_account(self) -> bool:
        """Adopt the linked account.

        0.14.8's listAccounts returns just {"number": ...} — no ACI. Keying
        "am I linked?" off the ACI alone reported a working account as unlinked
        and never opened the event stream. The number is a valid account handle,
        so it is the fallback; the ACI is then discovered from listContacts,
        which does carry our own row.
        """
        acct = signalrpc.account()
        if not acct:
            self.linked = False
            return False
        aci = signalrpc.aci_of(acct)
        number = str(first(acct, "number", default="") or "")
        handle = signalrpc.handle_of(acct)
        if not handle:
            self.linked = False
            return False
        if not aci:
            aci = signalrpc.discover_aci(handle, number)
        first_link = not self.linked
        self.self_aci, self.self_number, self.account, self.linked = aci, number, handle, True
        store.set_meta("self_aci", aci)
        store.set_meta("self_number", number)
        store.set_meta("account_handle", handle)
        store.set_meta("daemon_version", signalrpc.version())
        if first_link:
            store.log_event("linked", f"account={handle} aci={aci or 'unknown'}")
        return True

    def is_self(self, identifier: str | None) -> bool:
        """True for any id that is his: ACI, E164 number, or a typed variant."""
        if not identifier:
            return False
        probe = str(identifier).strip().lower()
        if not probe:
            return False
        candidates = {self.self_aci.lower(), self.self_number.lower(), self.account.lower()}
        candidates.discard("")
        if probe in candidates:
            return True
        digits = lambda v: "".join(c for c in str(v) if c.isdigit())  # noqa: E731
        if len(digits(probe)) >= 10:
            return any(digits(probe) == digits(c) for c in candidates if digits(c))
        return False

    def refresh_registry(self, *, force: bool = False) -> None:
        if not self.account:
            return
        now = int(time.time())
        if not force and now - self._last_registry_refresh < 900:
            return
        self._last_registry_refresh = now
        self.names.update(signalrpc.profile_names(self.account))
        for g in signalrpc.list_groups(self.account):
            gid = canonical_group_id(str(first(g, "groupId", "group_id", "id", default="")))
            title = str(first(g, "groupName", "group_name", "title", "name", default="") or "")
            if gid:
                if title:
                    self.groups[gid] = title
                store.upsert_chat(
                    f"grp:{gid}",
                    kind="group",
                    title=title,
                    muted=bool(first(g, "isMuted", "muted", default=False)),
                    members=len(first(g, "members", default=[]) or []),
                )

    # --- chat resolution ----------------------------------------------

    def chat_for(self, data: dict, envelope: dict, *, self_side: bool) -> tuple[str, str, str] | None:
        """(chat_id, kind, title) for one message, or None if it is noise."""
        ginfo = first(data, "groupInfo", "group_info", default=None)
        gid = canonical_group_id(str(first(ginfo or {}, "groupId", "group_id", default="")))
        if not gid:
            gid = canonical_group_id(str(first(envelope, "groupId", "group_id", default="")))
        if gid:
            title = str(first(ginfo or {}, "groupName", "group_name", default="") or self.groups.get(gid, "") or "")
            if title:
                self.groups.setdefault(gid, title)
            return f"grp:{gid}", "group", title

        # 1:1. Own the conversation from the other side of it.
        peer = ""
        if self_side:
            peer = str(first(data, "destinationUuid", "destination_uuid", default="") or "")
        else:
            peer = str(first(envelope, "sourceUuid", "source_uuid", default="") or "")
        if not peer:
            return None
        if self.is_self(peer):
            return "self", "self", "Note to self"
        title = str(first(envelope, "sourceName", "source_name", default="") or self.names.get(peer, "") or "")
        return f"dm:{peer}", "dm", title

    # --- one event ---------------------------------------------------

    def handle(self, event_id: str | None, payload: dict) -> None:
        env = payload.get("envelope") if isinstance(payload, dict) else None
        if not isinstance(env, dict):
            env = payload if isinstance(payload, dict) else {}
        self.last_event_id = event_id or self.last_event_id
        if event_id:
            store.set_meta("last_event_id", str(event_id))

        data = env.get("dataMessage") or env.get("data_message") or {}
        sync = env.get("syncMessage") or env.get("sync_message") or {}
        receipt = env.get("receiptMessage") or env.get("receipt_message") or {}

        if not isinstance(data, dict):
            data = {}
        if not isinstance(sync, dict):
            sync = {}
        if not isinstance(receipt, dict):
            receipt = {}

        # He read something on his phone. Mirror it so "unread" means unread.
        for rm in sync.get("readMessages") or sync.get("read_messages") or []:
            if not isinstance(rm, dict):
                continue
            sender = str(first(rm, "senderUuid", "sender_uuid", "sender", default="") or "")
            ts = int(first(rm, "timestamp", default=0) or 0)
            if sender:
                for cid in (f"dm:{sender}",):
                    store.mark_read(cid, ts)

        if receipt.get("isRead") or receipt.get("is_read"):
            store.log_event("read-receipt", str(first(receipt, "timestamps", default=[])[:4]))

        # Everything below is a message we might summarize.
        items: list[tuple[dict, bool]] = []
        if data:
            items.append((data, False))
        sent = sync.get("sentMessage") or sync.get("sent_message")
        if isinstance(sent, dict):
            items.append((sent, True))
        if not items:
            kind = "other"
            for key in ("storyMessage", "story_message", "callMessage", "call_message",
                        "typingMessage", "typing_message", "editMessage", "edit_message",
                        "receiptMessage", "receipt_message", "syncMessage", "sync_message"):
                if env.get(key):
                    kind = key
                    break
            self.envelope_kinds[kind] = self.envelope_kinds.get(kind, 0) + 1
            return

        for body, self_side in items:
            self.buffer(body, env, self_side=self_side)

    def buffer(self, data: dict, envelope: dict, *, self_side: bool) -> None:
        ts = int(first(data, "timestamp", default=0) or first(envelope, "timestamp", default=0) or 0)
        if not ts:
            return

        loc = self.chat_for(data, envelope, self_side=self_side)
        if not loc:
            return
        chat_id, kind, title = loc

        # Not messages: joins, leaves, timer changes, "is typing".
        if first(data, "isEndSession", "is_end_session") or first(data, "isExpirationUpdate", "is_expiration_update"):
            return
        if first(data, "groupInfo", "group_info") and not str(first(data, "message", default="") or ""):
            gtype = str(first(first(data, "groupInfo", "group_info", default={}) or {}, "type", default=""))
            if gtype and gtype.upper() not in ("DEFAULT", "UNKNOWN"):
                store.log_event("group-change", f"{chat_id} {gtype}")
                return
        if first(data, "reaction", default=None) and not str(first(data, "message", default="") or ""):
            return
        if first(data, "pollVote", "poll_vote") and not str(first(data, "message", default="") or ""):
            return

        text = str(first(data, "message", default="") or "")
        attachments = data.get("attachments") or []
        media, media_count = attachment_kind(attachments if isinstance(attachments, list) else [])
        if not text and not media:
            return

        aci = ""
        name = ""
        if self_side:
            aci = self.self_aci
            name = "You"
        else:
            aci = str(first(envelope, "sourceUuid", "source_uuid", default="") or "")
            name = str(first(envelope, "sourceName", "source_name", default="") or self.names.get(aci, "") or aci[:8])
            if self.is_self(aci):
                aci, name = self.self_aci or aci, "You"
                self_side = True

        quote = data.get("quote") if isinstance(data.get("quote"), dict) else {}
        quote_author = str(first(quote or {}, "authorUuid", "author_uuid", "authorNumber", "author", default="") or "")
        quote_text = str(first(quote or {}, "text", default="") or "")[:500]

        mention_entities = [m for m in (data.get("mentions") or []) if isinstance(m, dict)]
        hits = mentions.scan_envelope(
            text,
            mention_entities=mention_entities,
            quote_author_aci=quote_author or None,
            self_aci=self.self_aci or None,
            self_ids=[i for i in (self.self_aci, self.self_number, self.account) if i],
        )

        body = text if text else f"[{media}]"
        inserted = store.insert_message(
            chat_id=chat_id,
            ts=ts,
            sender_aci=aci,
            sender_name=name,
            text=body,
            is_self=self_side,
            is_reply=bool(quote_author and quote_author.lower() == (self.self_aci or "").lower()),
            mention_kinds=mentions.kinds(hits),
            quote_text=quote_text,
            attachments=media_count,
            event_id=int(self.last_event_id) if (self.last_event_id or "").isdigit() else None,
        )
        if inserted:
            store.upsert_chat(chat_id, kind=kind, title=title)
            store.touch_chat(chat_id, ts)
            store.note_seen(ts)
            if name and aci and aci not in self.names and not self_side:
                self.names[aci] = name
        self.envelope_kinds["message"] = self.envelope_kinds.get("message", 0) + (1 if inserted else 0)

    # --- loop ---------------------------------------------------------

    def _run(self) -> None:
        backoff = config.SSE_RECONNECT_MIN_WAIT
        while not self.stop.is_set():
            try:
                if not self.refresh_account():
                    self.linked = False
                    store.set_meta("link_state", "unlinked")
                    self.stop.wait(10)
                    continue
                self.refresh_registry(force=True)
                store.set_meta("link_state", "linked")
                self.connected = True
                backoff = config.SSE_RECONNECT_MIN_WAIT
                for event_id, payload in signalrpc.events(self.last_event_id, stop=self.stop):
                    if self.stop.is_set():
                        break
                    try:
                        self.handle(event_id, payload)
                    except Exception as exc:
                        self.errors += 1
                        # Type and message come FIRST: keeping only the tail of a
                        # formatted traceback buried the real exception, which is how
                        # the shadowed-method bug stayed invisible for so long.
                        store.log_event(
                            "handle-error",
                            f"{type(exc).__name__}: {exc} | {traceback.format_exc(limit=1).strip()[:140]}",
                        )
                self.connected = False
                store.log_event("stream", "events stream closed, reconnecting")
            except Exception as exc:
                self.connected = False
                self.errors += 1
                store.log_event("stream-error", str(exc)[:200])
            if self.stop.is_set():
                break
            self.stop.wait(backoff)
            backoff = min(config.SSE_RECONNECT_MAX_WAIT, backoff * 2)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="signal-ingest", daemon=True)
        self.thread.start()

    def status(self) -> dict:
        return {
            "linked": self.linked,
            "streaming": self.connected,
            "self_aci": self.self_aci,
            "number": self.self_number,

            "account": self.account or self.self_number or store.get_meta("account_handle"),
            "daemon_version": store.get_meta("daemon_version"),
            "last_event_id": self.last_event_id,
            "errors": self.errors,
            "envelope_kinds": self.envelope_kinds,
            "chats_known": len(self.groups) + len(self.names),
        }


INGEST = Ingest()
