"""The 8pm job.

    window (24h) -> every chat with activity -> one summary + one draft each
                 -> mention flags -> af_heart MP3 per chat -> wipe the buffer

Order matters at the end: audio is rendered *before* the buffer is wiped, because
the script it reads is derived from the summary (not the raw transcript), and the
wipe is a single decision made once everything good is already on disk.

Runs in two places: from cron (`bin/rollup --nightly`) and from the web button
(`server.py` in a thread). Same code, same lock.
"""

from __future__ import annotations

import json
import time
import traceback

from . import audio, config, llm, mentions, signalrpc, speech, store


def _title_for(chat_id: str, stored: str, ing) -> str:
    if stored:
        return stored
    if chat_id == "self":
        return "Note to self"
    if chat_id.startswith("grp:"):
        gid = chat_id[4:]
        return (ing.groups.get(gid) if ing else "") or "Signal group"
    peer = chat_id[4:]
    return (ing.names.get(peer) if ing else "") or peer[:8]


def _transcript(rows) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "text": r["text"] or "",
            "is_self": bool(r["is_self"]),
            "sender_name": r["sender_name"] or "",
            "quote_text": r["quote_text"] or "",
            "media": f"{r['attachments']} media" if r["attachments"] else "",
            "clock": store.clock_of(r["ts"]),
            "mention_kinds": [k for k in (r["mention_kinds"] or "").split(",") if k],
        })
    return out


def build(day: str | None = None, *, force: bool = False, speak: bool = True, from_ms: int | None = None, to_ms: int | None = None) -> dict:
    """One roll-up. Returns a run summary dict."""
    from .ingest import INGEST  # late import: the ingest thread owns the registries

    now_ms = int(time.time() * 1000)
    start, end = (from_ms, to_ms) if (from_ms and to_ms) else store.window_start_ms(now_ms)
    day = day or store.day_of(end)

    run_id = store.start_run(day, (start, end))
    if run_id is None:
        return {"ok": False, "status": "busy", "error": "a roll-up is already running"}

    ok = False
    chats = messages = mentioned_total = purged = 0
    detail: list[str] = []
    try:
        INGEST.refresh_account()
        INGEST.refresh_registry(force=True)

        rows = store.chats_in_window(start, end)
        chats = len(rows)
        if not chats:
            ok = True
            store.finish_run(run_id, "ok", chats=0, detail="quiet day, nothing in the window")
            return {"ok": True, "status": "ok", "chats": 0, "day": day, "detail": ["quiet day, nothing in the window"]}

        rendered: list[dict] = []
        for row in rows:
            messages += int(row["msg_count"])
            msgs = store.messages_for(row["chat_id"], start, end)
            transcript = _transcript(msgs)

            hits_by_message = [mentions.scan_text(m["text"]) for m in transcript]
            # mention_kinds is a list per message (see _transcript), so flatten it
            # rather than joining — joining lists raised deep in the roll-up.
            stored_kinds = [k for m in transcript for k in (m["mention_kinds"] or [])]
            labels = mentions.labels(hits_by_message)
            if "at-mention" in stored_kinds and "@mentioned" not in labels:
                labels.append("@mentioned")
            if "reply" in stored_kinds and "replied to you" not in labels:
                labels.append("replied to you")
            mentioned = int(row["mention_count"] or 0) or len([h for hits in hits_by_message for h in hits])
            if mentioned:
                mentioned_total += 1

            participants: dict[str, int] = {}
            for m in transcript:
                if m["is_self"]:
                    continue
                who = m["sender_name"] or "someone"
                participants[who] = participants.get(who, 0) + 1
            people = sorted(({"name": n, "count": c} for n, c in participants.items()), key=lambda p: -p["count"])

            title = _title_for(row["chat_id"], row["title"] or "", INGEST)
            summary, draft, engine = llm.write_rollup(
                title=title,
                kind=row["kind"],
                participants=people,
                messages=transcript,
                mentioned=bool(mentioned),
                mention_labels=labels,
                muted=bool(row["muted"]),
            )
            no_reply = not draft or draft.strip().upper().startswith("NONE")

            record = {
                "chat_id": row["chat_id"],
                "kind": row["kind"],
                "title": title,
                "summary": summary,
                "draft": "" if no_reply else draft,
                "needs_reply": 0 if no_reply else 1,
                "mentioned": int(mentioned or 0),
                "mention_labels": ",".join(labels[:4]),
                "msg_count": int(row["msg_count"]),
                "self_count": int(row["self_count"] or 0),
                "unread": store.unread_count(row["chat_id"], start, end, int(row["last_read_ts"] or 0)),
                "participants": json.dumps(people),
                "first_ts": int(row["first_ts"] or 0),
                "last_ts": int(row["last_ts"] or 0),
                "model": config.LLM_MODEL if engine == "llm" else "",
                "engine": engine,
            }
            # Spoken text is derived from the summary, never from the raw transcript,
            # so a wiped buffer cannot change what the audio says.
            record["speech_text"] = speech.chat_script(
                {**record, "no_reply": no_reply, "mention_labels": labels[:4], "participants": people}
            )

            if speak and config.TTS_ENABLED:
                try:
                    clip = audio.render_chat(day, record, force=force)
                    for key in ("audio_path", "audio_bytes", "audio_seconds"):
                        record[key] = clip.get(key, record.get(key, 0))
                except Exception as exc:
                    detail.append(f"audio {title}: {exc}")
                    store.log_event("audio-failed", f"{title}: {exc}")

            store.save_rollup(day, record)
            rendered.append({"chat_id": row["chat_id"], "title": title, "record": record})

        if speak and config.TTS_ENABLED and rendered:
            try:
                audio.render_intro(day, [{"title": r["title"], **r["record"]} for r in rendered])
            except Exception as exc:
                detail.append(f"intro audio: {exc}")
                store.log_event("audio-failed", f"intro: {exc}")

        if config.PURGE_RAW_AFTER_ROLLUP:
            purged = audio_purge()

        ok = True
        store.finish_run(
            run_id, "ok", chats=chats, messages=messages,
            mentioned=mentioned_total, purged=purged, detail="; ".join(detail)[:400],
        )
        store.log_event("rollup", f"{chats} chats, {messages} messages, {mentioned_total} mentions, {purged} purged")
        audio.prune(config.DAYS_SHOWN)
        return {
            "ok": True, "status": "ok", "day": day, "chats": chats, "messages": messages,
            "mentioned": mentioned_total, "purged": purged, "detail": detail,
        }
    except Exception as exc:
        store.finish_run(run_id, "failed", chats=chats, messages=messages, mentioned=mentioned_total,
                         error=str(exc)[:400], detail="; ".join(detail)[:200])
        store.log_event("rollup-failed", f"{exc}\n{traceback.format_exc(limit=3)}")
        return {"ok": False, "status": "failed", "error": str(exc), "day": day, "chats": chats}


def speech_script(record: dict, no_reply: bool, labels: list[str], title: str) -> str:
    """Spoken text for one chat. Summary plus draft, ear-friendly."""
    row = {**record, "no_reply": no_reply, "mention_labels": labels or [], "participants": []}
    try:
        from . import speech

        row["participants"] = [{"name": p, "count": 1} for p in (record.get("participants") or [])]
        if isinstance(record.get("participants"), str):
            import json as _json

            row["participants"] = _json.loads(record["participants"] or "[]")
        return speech.chat_script(row)
    except Exception:
        return f"{title}. {record.get('summary', '')}"


def audio_purge() -> int:
    """Wipe the buffered transcript text. The product is the summary."""
    return store.purge_buffer(None, vacuum=True)


def reachable() -> dict:
    """Cheap pre-flight, used by the button and /health."""
    out = {"signal": False, "voice": False, "llm": False}
    try:
        out["signal"] = bool(signalrpc.version())
    except Exception as exc:
        out["signal_error"] = str(exc)[:120]
    try:
        out["voice"] = tts_health()
    except Exception:
        pass
    try:
        out["llm"] = bool(config.llm_api_key())
    except Exception:
        pass
    return out


def tts_health() -> bool:
    try:
        from . import tts

        return str(tts.health().get("status", "")) == "ok"
    except Exception:
        return False
