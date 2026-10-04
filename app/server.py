"""Web server for signal-summarizer.home.arpa.

Stdlib http.server, no dependencies. Serves:

    /                     the phone-first app (basic-auth gated)
    /link               QR pairing page, auto-refreshes the 60-second link URI
    /api/state          everything the app renders
    /api/day/<day>    a past night's roll-ups
    /api/run          POST: run the roll-up now (same lock as cron)
    /api/read/<id>    POST: mark a chat read
    /api/say/<id>     POST: re-synthesize one chat's clip
    /audio/<d>/<f>    MP3 with Range support — iOS Safari will not play otherwise
    /health           for the status board

The ingest thread lives here: the daemon is the source of truth, but the buffer
this process writes is what the 8pm job reads.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import audio, config, rollup, signalrpc, store  # noqa: E402
from app.ingest import INGEST  # noqa: E402

PUBLIC = Path(__file__).resolve().parent.parent / "public"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AUDIO_FILE_RE = re.compile(r"^(?:[a-f0-9]{16}|intro)\.mp3$")
_BUILD_LOCK = threading.Lock()
_BUILD = {"running": False, "started": None, "last": None, "result": None}
_LINK = {"state": "idle", "uri": "", "at": 0, "detail": "", "aci": ""}
_LINK_LOCK = threading.Lock()


def build_now(*, force: bool = False, speak: bool = True) -> dict:
    """Serialize builds: cron, the button and boot catch-up cannot race."""
    if not _BUILD_LOCK.acquire(blocking=False):
        return {"ok": False, "status": "busy", "error": "a roll-up is already running"}
    _BUILD.update(running=True, started=time.time(), result=None)
    try:
        result = rollup.build(force=force, speak=speak)
        _BUILD["result"] = result
        return result
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "status": "failed", "error": str(exc)[:300]}
    finally:
        _BUILD.update(running=False, last=time.time())
        _BUILD_LOCK.release()


def _build_thread(**kw) -> None:
    threading.Thread(target=build_now, kwargs=kw, name="rollup", daemon=True).start()


def _link_worker(uri: str) -> None:
    """Blocks on finishLink until the iPhone answers or the URI expires."""
    try:
        res = signalrpc.finish_link(uri)
        # refresh_account() reads the daemon's own view, which is authoritative
        # and gives us the number when finishLink returns a thin result.
        INGEST.refresh_account()
        aci = INGEST.self_aci or signalrpc.aci_of(res)
        with _LINK_LOCK:
            if _LINK.get("uri") == uri:
                _LINK.update(state="linked", aci=aci, detail="linked", at=time.time())
        store.log_event("link", f"paired handle={INGEST.handle or '?'} aci={aci or 'unknown'}")
        INGEST.refresh_registry(force=True)
        _build_thread(force=True)  # first roll-up immediately after pairing
    except Exception as exc:
        with _LINK_LOCK:
            if _LINK.get("uri") == uri:
                _LINK.update(state="waiting", detail=f"still waiting: {str(exc)[:120]}", at=time.time())
        store.log_event("link-wait", str(exc)[:160])


def link_start() -> dict:
    with _LINK_LOCK:
        if _LINK.get("state") == "linked":
            return dict(_LINK)
        try:
            uri = signalrpc.start_link()
        except Exception as exc:
            _LINK.update(state="error", detail=str(exc)[:200], at=time.time())
            return dict(_LINK)
        _LINK.update(state="waiting", uri=uri, at=time.time(), detail="scan with your iPhone")
    threading.Thread(target=_link_worker, args=(uri,), name="link", daemon=True).start()
    return dict(_LINK)


def link_status() -> dict:
    # Re-ask the daemon: it may have finished provisioning after the page last
    # polled, and the pairing thread may already be gone.
    if not INGEST.linked:
        try:
            INGEST.refresh_account()
        except Exception:
            pass
    if INGEST.linked:
        with _LINK_LOCK:
            _LINK.update(state="linked", aci=INGEST.self_aci, at=time.time())
    out = dict(_LINK)
    out["linked"] = INGEST.linked
    out["account"] = INGEST.self_number or store.get_meta("self_aci")
    # The URI is dead after ~60s; tell the page to stop showing a live QR.
    out["uri_age"] = round(time.time() - float(out.get("at") or 0), 1)
    out["uri_expired"] = bool(out.get("uri")) and out["uri_age"] > 55 and out["state"] != "linked"
    return out


def state_payload(day: str | None = None) -> dict:
    day = day or store.day_of()
    rows = store.rollups_for(day)
    briefing = store.get_briefing(day) or {}
    run = store.last_run()
    total_audio = sum(float(r.get("audio_seconds") or 0) for r in rows)
    return {
        "day": day,
        "human_date": store.day_of_long(day),
        "days": store.days_available(),
        "briefing": {
            "speech_text": briefing.get("speech_text", ""),
            "audio_url": (audio.intro_url(day, int(briefing.get("audio_bytes") or 0))
                          if briefing.get("audio_path") else ""),
            "audio_seconds": float(briefing.get("audio_seconds") or 0),
            "voice": briefing.get("voice") or config.TTS_VOICE,
        },
        "chats": [{
            "chat_id": r["chat_id"],
            "kind": r["kind"],
            "title": r["title"],
            "summary": r["summary"],
            "draft": r["draft"],
            "needs_reply": bool(r["needs_reply"]),
            "mentioned": int(r["mentioned"] or 0),
            "mention_labels": r["mention_labels"],
            "msg_count": int(r["msg_count"] or 0),
            "self_count": int(r["self_count"] or 0),
            "unread": int(r.get("unread") or 0),
            "participants": r["participants"],
            "first_ts": int(r.get("first_ts") or 0),
            "last_ts": int(r.get("last_ts") or 0),
            "audio_url": (audio.chat_url(day, r["chat_id"], int(r.get("audio_bytes") or 0))
                          if r.get("audio_path") else ""),
            "audio_seconds": float(r.get("audio_seconds") or 0),
            "first_clock": store.clock_of(r.get("first_ts")),
            "last_clock": store.clock_of(r.get("last_ts")),
            "engine": r.get("engine") or "",
        } for r in rows],
        "totals": {
            "chats": len(rows),
            "messages": sum(int(r["msg_count"] or 0) for r in rows),
            "mentioned": sum(1 for r in rows if r.get("mentioned")),
            "needs_reply": sum(1 for r in rows if r.get("needs_reply")),
            "audio_seconds": round(total_audio, 1),
        },
        "run": run,
        "building": _BUILD["running"],
        "next_run": store.next_run_ms(),
        "run_at": config.RUN_AT,
        "window_hours": config.WINDOW_HOURS,
        "tz": config.TZ,
        "signal": INGEST.status(),
        "voice": {"url": config.TTS_URL, "voice": config.TTS_VOICE, "enabled": config.TTS_ENABLED},
        "privacy": store.buffer_stats(),
        "model": config.LLM_MODEL,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SignalSummarizer/1.0"
    protocol_version = "HTTP/1.1"

    # --- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args) -> None:  # noqa: A003
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _authed(self) -> bool:
        if config.ALLOW_UNAUTHENTICATED or not config.AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
        except Exception:
            return False
        return user == config.AUTH_USER and password == config.AUTH_PASSWORD

    def _challenge(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="Signal roll-up", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = "Sign in to read your Signal roll-up."
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        extra = dict(extra or {})
        # Errors must never be cached. Without this a transient 404 on an audio
        # clip is cached by the browser heuristically, and the clip stays
        # "unable to load" long after the file exists on disk.
        if code >= 400:
            extra.setdefault("Cache-Control", "no-store")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # --- audio (Range is mandatory for iOS) ---------------------------

    def _audio(self, path: Path) -> None:
        if not path.is_file():
            return self._send(404, b"no such clip", "text/plain")
        size = path.stat().st_size
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            start = min(start, end)
            length = end - start + 1
            with open(path, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(length)
            self.send_response(206)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(chunk)
            return
        with open(path, "rb") as fh:
            self._send(200, fh.read(), "audio/mpeg", {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=604800"})

    def _static(self, rel: str) -> None:
        rel = (rel or "index.html").lstrip("/")
        target = (PUBLIC / rel).resolve()
        if not str(target).startswith(str(PUBLIC.resolve())) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".css":
            ctype = "text/css"
        if target.suffix == ".js":
            ctype = "text/javascript"
        cache = "public, max-age=604800" if target.name != "index.html" else "no-cache"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), f"{ctype}; charset=utf-8", {"Cache-Control": cache})

    # --- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except Exception:
            store.log_event("http-error", traceback.format_exc(limit=3).strip()[-300:])
            self._json({"ok": False, "error": "internal"}, 500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except Exception:
            store.log_event("http-error", traceback.format_exc(limit=3).strip()[-300:])
            self._json({"ok": False, "error": "internal"}, 500)

    def _route_get(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            return self._json({"status": "ok", "t": int(time.time() * 1000), "linked": INGEST.linked,
                               "day": store.day_of()})
        if path == "/api/health":
            return self._json({"status": "ok", **rollup.reachable()})
        if path == "/api/signal-health":
            # Lets the status board see the daemon without ever opening a socket
            # to it: signal-cli is on an internal-only network the dashboard has
            # no route to. Liveness and version only, never message content.
            out = {"status": "unreachable", "version": "", "accounts": 0}
            try:
                out["version"] = signalrpc.version()
                accounts = signalrpc.list_accounts()
                out["accounts"] = len(accounts)
                out["linked"] = bool(accounts)
                out["status"] = "ok" if out["version"] else "no version"
            except Exception as exc:
                out["detail"] = str(exc)[:160]
            return self._json(out)
        if not self._authed():
            return self._challenge()

        if path in ("/", "/roll-up", "/rollup"):
            return self._static("index.html")
        if path == "/link":
            return self._static("link.html")
        if path in ("/app.js", "/app.css", "/link.js", "/qrcode.min.js"):
            return self._static(path.lstrip("/"))
        if path == "/api/state":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._json(state_payload((q.get("day") or [None])[0]))
        if path.startswith("/api/day/"):
            day = path.split("/")[-1]
            if not _DAY_RE.match(day):
                return self._json({"ok": False, "error": "bad day"}, 400)
            return self._json(state_payload(day))
        if path == "/api/link/status":
            return self._json(link_status())
        # The app polls this with fetch(), which is a GET. It lived only in
        # _route_post, so "Run it now" polled a 404 forever and never learned
        # the build had finished.
        if path == "/api/build-status":
            return self._json({"running": _BUILD["running"], "started": _BUILD["started"],
                               "last": _BUILD["last"], "result": _BUILD["result"]})
        if path == "/api/events":
            return self._json({"events": store.recent_events(30)})
        if path.startswith("/audio/"):
            parts = [p for p in path.split("/") if p]
            # The URL carries the .mp3 extension (audio.chat_url generates it), so the
            # guard has to match the whole filename. An anchored 16-hex pattern here
            # rejected every real clip with a 404 while the file sat on disk.
            if len(parts) == 3 and _DAY_RE.match(parts[1]) and _AUDIO_FILE_RE.match(parts[2]):
                return self._audio(Path(config.AUDIO_DIR) / parts[1] / parts[2])
            return self._send(404, b"bad clip", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def _route_post(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path != "/health" and not self._authed():
            return self._challenge()

        if path == "/api/run":
            body = self._body()
            if _BUILD["running"]:
                return self._json({"ok": False, "status": "busy"})
            _build_thread(force=bool(body.get("force", True)), speak=bool(body.get("speak", True)))
            return self._json({"ok": True, "status": "started"})
        if path == "/api/build-status":
            return self._json({"running": _BUILD["running"], "started": _BUILD["started"],
                               "last": _BUILD["last"], "result": _BUILD["result"]})
        if path == "/api/link/start":
            return self._json(link_start())
        if path == "/api/link/status":
            return self._json(link_status())
        if path.startswith("/api/read/"):
            cid = urllib.parse.unquote(path.split("/api/read/", 1)[1])
            if not cid:
                return self._json({"ok": False, "error": "no chat"}, 400)
            store.mark_read(cid, int(time.time() * 1000))
            return self._json({"ok": True, "chat_id": cid})
        if path.startswith("/api/say/"):
            cid = urllib.parse.unquote(path.split("/api/say/", 1)[1])
            day = store.day_of()
            rows = [r for r in store.rollups_for(day) if r["chat_id"] == cid]
            if not rows:
                return self._json({"ok": False, "error": "no roll-up for that chat today"}, 404)
            try:
                clip = audio.render_chat(day, dict(rows[0]), force=True)
                store.save_rollup(day, {**rows[0], "audio_path": clip.get("audio_path", ""),
                                         "audio_bytes": clip.get("audio_bytes", 0),
                                         "audio_seconds": clip.get("audio_seconds", 0)})
                return self._json({"ok": True, **clip, "url": audio.chat_url(day, cid)})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:200]}, 502)
        if path == "/api/read-all":
            now = int(time.time() * 1000)
            for r in store.rollups_for(store.day_of()):
                store.mark_read(r["chat_id"], now)
            return self._json({"ok": True})
        return self._send(404, b"not found", "text/plain")


def catch_up() -> None:
    """If we booted past RUN_AT with nothing built for today, build it."""
    if not config.CATCH_UP_ON_BOOT or not INGEST.linked:
        return
    now = time.localtime()
    hh, mm = (int(x) for x in config.RUN_AT.split(":"))
    if (now.tm_hour, now.tm_min) < (hh, mm):
        return
    if store.last_run() and store.day_of((store.last_run() or {}).get("finished_at") or 0) == store.day_of():
        return
    store.log_event("catch-up", f"booting past {config.RUN_AT} with no roll-up for today")
    _build_thread(force=False)


def serve() -> None:
    if not config.AUTH_PASSWORD and not config.ALLOW_UNAUTHENTICATED:
        print("REFUSING to serve: SS_AUTH_PASSWORD is empty. Set it in .env, or opt in to "
              "SS_ALLOW_UNAUTHENTICATED=1 if this box is tailnet-only on purpose.", file=sys.stderr)
        raise SystemExit(2)
    store.init()
    INGEST.start()
    threading.Thread(target=catch_up, name="catch-up", daemon=True).start()
    httpd = ThreadingHTTPServer((config.WEB_HOST, config.WEB_PORT), Handler)
    httpd.daemon_threads = True
    print(f"signal-summarizer on :{config.WEB_PORT}  ({'basic-auth' if config.AUTH_PASSWORD else 'OPEN'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    serve()
