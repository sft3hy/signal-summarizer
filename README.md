# signal-summarizer

Every night at 20:00 this reads the last 24 hours of your Signal, writes one
summary per group chat and DM, flags the ones where you were mentioned by name,
drafts a reply you can copy, and renders it all as audio in `af_heart`'s voice.

**https://signal-summarizer.home.arpa** · sign in as `signal` (password in `.env`)

```
iPhone ──scan once──┐
                    ▼
 signal-summarizer :8080 ──JSON-RPC + SSE──> signal-cli 0.14.8 :8080
   web · cron 20:00 · SQLite            keys live ONLY here
   │
   ├── omlx :8000      summaries + drafted replies
   └── voice :8001     af_heart, one MP3 per chat
```

## Why it is built this way

- **signal-cli is a linked device, never a registration.** `register` unregisters
  your phone. `link` adds a second device. Your iPhone stays primary and holds the
  kill switch: Settings → Account → Linked Devices → unlink.
- **Two containers, one privileged boundary.** `signal-cli` is not on the `edge`
  network and publishes no ports, so nothing on the tailnet can open a socket to
  the process holding your account keys. The web app is the only thing that can
  reach it, and it is the only thing Traefik routes to.
- **Nothing runs as root.** vixie-cron needs root to read `/etc/cron.d`, so the
  crontab names `summarizer` in its user field and the entrypoint drops privileges
  with `setpriv` before exec.
- **Raw text does not outlive its use.** Messages are buffered because the 8pm job
  needs something to read, then deleted and VACUUMed in the same run. Attachments,
  avatars and stickers are never downloaded, so a roll-up says "three photos" and
  stops there. `/api/state` reports the live buffer count.

## First run

```bash
cd ~/dev/signal-summarizer
cp .env.example .env && chmod 600 .env      # set SS_LLM_API_KEY + SS_AUTH_PASSWORD
docker compose up -d --build
open https://signal-summarizer.home.arpa/link   # scan with your iPhone
```

The link URI dies in about 60 seconds, so `/link` mints a new one every 45 seconds
and shows a fuse bar draining. After pairing it runs the first roll-up immediately.

Edge wiring (DNS record + status board card + arrows) lives in `~/homelab/edge`:

```bash
cd ~/homelab/edge && bash scripts/05-signal-summarizer.sh --up
```

## Day 2

| | |
|---|---|
| Roll-up | `docker compose exec signal-summarizer /app/bin/rollup` — or press "Run it now" in the app |
| Skip audio | `/app/bin/rollup --no-audio` (voice box busy or down) |
| Shorter window | `/app/bin/rollup --hours 6` |
| Diagnostics | `docker compose exec signal-summarizer python3 /app/bin/check` |
| Wipe everything | `docker compose down -v` removes both volumes, keys and all |
| Update | bump `SIGNAL_CLI_VERSION` + its sha256 in `signal-cli/Dockerfile`, then `docker compose up -d --build` |

**signal-cli expires.** Upstream says releases older than three months may stop
working. The board card shows the running version; watch it.

## The mention rule

Flags: `Sam Townsend`, `Samuel Townsend`, `Sam T`, the listed misspellings,
`slammy`, real `@`mentions matched on your ACI, and replies that quote one of your
messages. **A bare `Sam` never flags**, and neither does a Sam with any other
surname — `Samir Townsend`, `Sam Tang`, `Sam the barista` and `sam technology` are
all in `tests/test_core.py` as failing cases on purpose.

Knobs live in `.env`: `SS_MENTION_FULL_NAMES`, `SS_MENTION_FIRSTS`,
`SS_MENTION_INITIAL`, `SS_MENTION_NICKNAMES`, `SS_MENTION_REPLIES`.

## Layout

```
app/
  config.py     env-driven knobs; defaults are this Studio's real values
  mentions.py   the name rule, and the tests that guard it
  ingest.py     SSE consumer; envelope parser built on signal-cli's published schemas
  store.py      SQLite: buffered messages (purged) + rollups (kept) + runs
  llm.py        omlx client, sentinel-delimited output, mechanical fallback
  speech.py     turns a roll-up into text that reads well aloud
  tts.py        af_heart client, streams to a .part file then renames
  audio.py      one MP3 per chat, cached and pruned
  rollup.py     the 8pm job, same code path as the web button
  server.py     basic-auth HTTP, Range-aware audio, QR pairing
bin/rollup      cron entry (0 20 * * *, America/Los_Angeles)
bin/preview     fake-data UI preview + headless screenshots, touches nothing live
tests/          python3 -m unittest discover -s tests
```

## Reading the design decisions

`DESIGN.md` records the palette, type roles, and why the waveform is the interface
rather than decoration: every bar is a peak measured from that chat's real MP3.
