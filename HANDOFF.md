# HANDOFF — signal-summarizer

**For:** the next agent, fresh context window.
**As of:** 2026-10-03 ~17:50 America/Los_Angeles.
**State: running, healthy, linked, one thing left to confirm.** Read the "Traps"
section before you debug anything — three of them cost hours and will bite again.

---

## TL;DR

Nightly Signal roll-up: at 20:00 it reads the last 24h, writes one summary per
group chat and DM, flags mentions of **Sam Townsend / Samuel Townsend**, drafts a
reply, and renders each chat as an MP3 in `af_heart`'s voice.

Everything is deployed and verified except: **no real Signal message has arrived
since the ingest fix went live.** The pipeline is proven with real omlx + real
af_heart on synthetic data, but the live SSE path has not yet carried a real
message. That is the single open item, and it needs ~10 seconds of the user.

**Do not re-investigate the 12 bugs in "Bugs fixed" — they are closed.**

---

## Access

| | |
|---|---|
| App | `https://signal-summarizer.home.arpa` (username `signal`) |
| Pairing | `https://signal-summarizer.home.arpa/link` — mints a new QR every 45 s |
| Password | in `~/dev/signal-summarizer/.env` → `SS_AUTH_PASSWORD` (gitignored). Read it from there; never copy it into a doc, commit, or chat log. |
| Repos | app: `~/dev/signal-summarizer` · edge wiring: `~/homelab/edge` |
| Git | **nothing committed** except the initial README. `.gitignore` was created during this session — commit early. |

**Account:** linked as his **personal cell** (the exact number, ACI and Signal
username live in the container's `meta` table and `docker compose exec signal-cli
signal-cli listAccounts` output — deliberately not written down here). The Google
Voice number he mentioned early on belongs to a **different, bot account** — he
mis-stated it. This service must use only the personal cell. Never register it.

---

## Verified working (evidence, not assumptions)

- **Containers:** `signal-cli` and `signal-summarizer` both `running healthy`.
- **signal-cli 0.14.8 on arm64** — built image, `version` RPC returns `0.14.8`.
- **Link persistence** — survived **six** container recreations; keys live in the
  named volume `signal-summarizer_signal-cli-data`.
- **Live ingest thread:** `linked: true streaming: true`.
- **TLS:** issuer `Studio Home CA Intermediate CA`, verified with **no `-k`**
  (`ssl_verify_result: 0`).
- **Auth:** 401 unauthenticated, 200 authenticated. All routes 200 without
  `--resolve`: `/health / /link /app.css /app.js /api/state /api/build-status`.
  Cert valid >24 h.
- **`/api/build-status` (GET):** 200. Zero 404s in Traefik logs since the fix.
- **Voice:** `http://host.docker.internal:8001/health` → `ok`, backend `mlx`.
- **Model:** `Qwen3.8-Flash-Next-oQ4e-mtp` at `100.122.197.81:8000/v1`, key present.
- **Full pipeline, real omlx + real af_heart, scratch dir:**
  ```
  BUILD ok: True | chats 1 | purged 4
  chat : Studio Build Crew | mentioned 1 | unread 3 | needs_reply 1 | engine llm
  lbls : ['Sam Townsend', '@mentioned']
  SUMM : Sam, you already handled Dana's lumber invoice approval and Ike's Tuesday
         signature request. Dana also flagged that the gate code needs changing this
         month, but no one assigned it or set a date. That is the only open item…
  DRAFT: Got it. Invoice approved. I'll be there Tuesday to sign for the booth.
         Dana, who's handling the gate code change and when do you want it done?
  AUDIO: 791040 bytes / 49.4 s
  buffer after build (must be 0): 0
  ```
- **Tests:** 35/35 `python3 -m unittest discover -s tests`, including the cases
  that must **never** flag: `Samir Townsend`, `Sam Tang`, `Sam the barista`,
  `sam technology`, bare `Sam`.
- **Edge:** Pi-hole record published; Traefik router live; dashboard shows the
  `Messaging` line (`#ef6a4d`), both stations, all six arrows, and container CPU/mem.

---

## The one open item

**Send one fresh Signal message, then run the roll-up.**

Earlier notes he sent were delivered, acked and consumed by the daemon while
`handle()` was crashing — they are gone and cannot be recovered (see "No backfill").

```bash
cd ~/dev/signal-summarizer
# user sends himself a note, or replies in any group, then:
docker compose exec -T -u summarizer signal-summarizer /app/bin/rollup   # -u matters, see bug 13
docker compose exec -T signal-summarizer python3 /app/bin/check
```

Expect: `buffered` rises above 0 before the build, a chat entry appears, and
`/api/state` returns non-empty `chats` with `audio_url` + `audio_seconds`.

Also still unverified in a **real browser**: blob playback + pause, the WebAudio
waveform peaks, the `/link` QR page rendering, and past-day navigation (needs >1
night of data).

---

## Bugs found and fixed — CLOSED, do not re-investigate

1. **`config._env()` ignored its default** when the var was unset (`os.environ.get(x) or ""`),
   so `float("")` raised `ValueError` at import.
2. **`ingest` → `scan_envelope(mention_acis=…)`** but the signature was `mention_ranges`
   → `TypeError` on every message.
3. **`self.handle` attribute shadowed the `Ingest.handle()` method.** Every event
   raised `TypeError: 'str' object is not callable`; the buffer stayed empty forever.
   Renamed to `self.account`. Regression tests now assert no attribute shadows a method.
4. **0.14.8 `listAccounts` returns only `{"number": …}` — no ACI.** "Am I linked?"
   keyed on the ACI, so a working account reported *unlinked* and the SSE stream
   never opened. Fixed: `handle_of()` (aci ∨ number) + `discover_aci()` which finds
   our own row in `listContacts`. This is why he saw "nothing to read".
5. **`VACUUM` inside a transaction fails** → the privacy wipe rolled back and would
   have retained every raw message indefinitely while reporting a clean purge.
   Now VACUUMs on its own `isolation_level=None` connection.
6. **`mark_read` argument order swapped** (`now, chat_id` vs `chat_id=? AND last_ts<=?`)
   → unread badge never cleared.
7. **`rollup.build` joined a list of lists** (`" ".join(m["mention_kinds"])`) →
   `TypeError: sequence item 0: expected str instance, list found`. Flattened.
8. **`insert_message` mangled a string arg:** `",".join("name")` stored
   `'n,a,m,e'`. Now normalises str ∨ list.
9. **`/api/build-status` was POST-only; the JS polls with GET** → permanent 404,
   "Run it now" never resolved. Registered for GET too, and the poll now checks `r.ok`.
10. **`rollup.build` returned `detail` as a str on one path, a list on another** →
    printed `note: d / note: a / note: y`.
11. **Logging hid the exception:** `traceback.format_exc()[-300:]` kept the *tail*,
    losing the message. Now `f"{type(exc).__name__}: {exc}"` first. This is what
    finally surfaced bug 3.
13. **Root-owned `/data/audio/<day>/`** — running `/app/bin/rollup` via
    `docker compose exec` runs as **root**, so the day folder was root-owned and the
    real server process (demoted to `summarizer`) could not write a single clip.
    Every night silently shipped with **no audio** (`Errno 13` on `.part`). Fixed:
    entrypoint `chown -R summarizer /data` at every boot. **Use
    `docker compose exec -u summarizer` for manual runs.**
14. **Every audio clip 404'd while the file existed on disk.** `_SLUG_RE` was
    anchored to a bare 16-hex slug, but `audio.chat_url()` emits `<slug>.mp3`.
    Now `_AUDIO_FILE_RE = ^(?:[a-f0-9]{16}|intro)\.mp3$`, with traversal tests.
15. **Browser cached the 404.** Error responses carried no `Cache-Control`, so the
    404 from bug 14 was heuristically cached and the clip stayed "unable to load"
    after it was fixed. Three-layer fix: `Cache-Control: no-store` on all 4xx,
    `?v=<bytes>` cache-buster on audio URLs, and the client drops `force-cache`
    and refetches with `cache: 'reload'` on retry.

**Verified audio:** `/audio/2026-10-03/40380bc1d358a6f8.mp3?v=363648` → 200
`audio/mpeg`, `accept-ranges: bytes`, 363648 B; ffprobe: mp3 24000 Hz mono
22.728 s 128k. Missing clips now answer `cache-control: no-store`.

12. **Mention engine rebuilt:** `(?![\w-])` blocked `Sam Townsend-Whitfield`; the
    trailing guard rejected `Sam T.`; misspelled first names with a correct surname
    didn't match; and Signal's leading `@` inside `mentions[]` covered text tripped
    our own lookbehind. Full-name pattern is now built from first-name ∨ typo sets
    with an optional middle initial.

---

## Traps that cost real time — read first

- **`docker compose exec` runs in the ALREADY-RUNNING container. It does not pick up
  edited source.** I chased a "the fix didn't work" ghost twice for this. Always
  `docker compose up -d --build` before you exec and conclude anything.
- **Chrome headless `--window-size` has a 500px minimum width.** Screenshots taken at
  390/430 are 500px renders cropped to size, which looks exactly like horizontal
  overflow that does not exist. Confirm with `scrollWidth <= innerWidth`, not eyes.
- **Host `:8080` is Pi-hole, host `:8099` is the news app.** Never bind either.
  The UI preview uses **8123**.
- **macOS caches NXDOMAIN.** Right after you add a `home.arpa` name, `dig @127.0.0.1`
  proves Pi-hole answers while `curl` still says "Could not resolve host" — the
  negative cache. Wait it out, or use
  `curl --resolve signal-summarizer.home.arpa:443:100.122.197.81`. **This has now
  cleared**: every route returns 200 with no `--resolve`. Phones query Pi-hole over
  the tailnet directly and were never affected.
- **Traefik hot-reloads routers but does not enqueue ACME SANs from a hot-reloaded
  file.** A new `Host()` gets `TRAEFIK DEFAULT CERT` until you
  `cd ~/homelab/edge && docker compose restart traefik`.
- **SQLite WAL:** opening the daemon's `account.db` from a read-only mount fails
  (SQLite must write `-shm`). Copy to a writable temp dir first.
- **The voice gateway has ONE worker and ONE voice (`af_heart`).** `POST /speak
  {text, voice, speed}` streams MP3 with **no Content-Length**. Synthesize serially;
  never fan out. `MAX_CHUNK_CHARS=420` server-side.
- **zsh does not word-split unquoted variables.** `R="--resolve host:443:ip"; curl $R`
  → "option is unknown". Inline the flags or use an array.
- **`docker compose exec` has no `-v`.** Scratch dirs must be created *inside* the
  container (`sh -c 'mkdir -p /tmp/x && …'`).

---

## No message backfill — settled, do not retry

- `account.db` in the daemon volume contains **zero tables** (verified directly).
  signal-cli keeps no local message archive.
- The Signal server delivers only **unread** envelopes for that device. Messages
  already read on his phone are never replayed to a linked device.
- `/api/v1/events` does **not** replay to a fresh connection (verified: bogus
  `Last-Event-ID` returns nothing, only `:` keepalives at 15 s).

**Consequence:** our own SQLite buffer *is* the archive. The 24h window fills from
the moment the device is linked and receiving, so the first roll-up after any
long outage can legitimately be thin. That is a property of Signal, not a bug.
Never weaken the purge to fake richer history — the buffer survives restarts on a
named volume, which is the correct mitigation.

---

## Polish backlog (none blocking)

1. **Summary tail is verbose** — it restates facts already covered. Tighten
   `llm.TASK`: hard word cap, or add "do not restate a fact already stated".
2. Summary opens `Sam, you already handled…` — decide whether summaries should
   address him by name at all.
3. `envelope_kinds` counts only dropped noise, never handled messages — an
   observability gap when diagnosing "why is this empty".
4. `errors` is cumulative per process and never resets, so `/api/state` shows
   stale restart noise forever.
5. `bin/check` flags the last run as a failure even if it's long superseded;
   only surface failures inside ~26 h.
6. Re-take `bin/preview` screenshots — not done since the CSS restructure.
7. WebAudio waveform peaks never verified in a real browser with real clips.
8. Commit. Nothing is in git yet beyond the initial README.
9. Consider surfacing `last_read_ts` ("seen 19:04") in the UI.

---

## File map

```
app/
  config.py     every knob, env-first; defaults are this Studio's real values
  mentions.py   THE name rule + the typo sets. Touch only with tests.
  ingest.py     SSE consumer + envelope parser (0.14.8 schemas). handle() is a
                METHOD — never add self.handle as an attribute.
  store.py      SQLite: messages (buffered→purged), rollups (kept), runs, events
  llm.py        omlx client, ===SUMMARY===/===DRAFT=== sentinels, mechanical fallback
  speech.py     prose→speech normalisation (no markup/emoji/URLs, ends with '.')
  tts.py        af_heart client; writes .part then renames
  audio.py      one MP3 per chat, /audio/<day>/<sha1[:16]>.mp3, pruned
  rollup.py     the 20:00 job. SAME code path as the web button.
  server.py     basic auth, Range audio, QR pairing, /api/*
public/  index.html app.css app.js link.html link.js qrcode.min.js (vendored)
bin/     rollup (cron entry)  check (diagnostics)  preview (UI screenshots)
         preview-seed (fake data, refuses to touch the live dir)
cron/rollup.cron   0 20 * * * summarizer …
signal-cli/        Dockerfile (0.14.8 + sha256 + arm64 libsignal) entrypoint.sh
tests/test_core.py 35 tests
DESIGN.md  palette, type roles, why the waveform is the interface
```

**Isolation, do not weaken:** `signal-cli` is on `signalbox` only, publishes no
ports; the app is on `edge` + `signalbox` and is the only route to it; the board
probes the daemon **through** the app at `/api/signal-health`, never directly.

---

## Commands

```bash
cd ~/dev/signal-summarizer
docker compose up -d --build          # ALWAYS --build after editing source
docker compose exec -T signal-summarizer python3 /app/bin/check
docker compose exec -T -u summarizer signal-summarizer /app/bin/rollup   # -u matters, see bug 13
docker compose exec -T -u summarizer signal-summarizer /app/bin/rollup   # -u matters, see bug 13 --no-audio
docker compose exec -T -u summarizer signal-summarizer /app/bin/rollup   # -u matters, see bug 13 --hours 6
./bin/preview                        # scratch UI + headless screenshots, touches nothing live
python3 -m unittest discover -s tests
docker compose down -v               # nuke both volumes = keys + all data
```

Edge wiring: `cd ~/homelab/edge && bash scripts/05-signal-summarizer.sh [--up]`
— verified passing (DNS published, board rebuilt, `/health` 200, authed `/` 200).
It now pulls `SS_AUTH_PASSWORD` out of the app's `.env`; it used to source only
`edge/.env`, which made the auth probe send an empty password and report a failure
that wasn't real.

---

## Guardrails

- **Never `signal-cli register`.** It unregisters his phone. Link only.
- **Never send a Signal message on his account without asking.** Drafts are
  copy-only by design; there is no send endpoint. Keep it that way.
- **Raw text purge stays.** Buffer only long enough to summarize, then VACUUM.
- **No published ports for `signal-cli`.**
- **`.env` never in git.** It holds the live password and the omlx key.
- Update `SIGNAL_CLI_VERSION` **and** its `SIGNAL_CLI_SHA256` together in
  `signal-cli/Dockerfile`; upstream breaks releases older than ~3 months, and the
  arm64 `libsignal_jni.so` version is derived from the bundled jar name.
