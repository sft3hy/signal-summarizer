# Design notes — signal-summarizer.home.arpa

Subject: one person's private Signal digest, read aloud by `af_heart`, at 20:00,
on a phone. The primary job is not "monitor" — it's *listen, then reply*.

## Tokens

### Colour
| token | hex | role |
|---|---|---|
| `--night` | `#0d1420` | base. Blue-black ink, deliberately blue so it does not read dead at 8pm |
| `--panel` | `#121b29` | the player surface only |
| `--rule` | `#23304a` | hairlines, gutter rules |
| `--chalk` | `#e9eff9` | primary text |
| `--ash` | `#93a2bd` | secondary text, metadata |
| `--flare` | `#ef6a4d` | **lifted from the "Messaging" line colour on status.home.arpa** — the app and its board station share one accent so the two surfaces are visibly the same service |
| `--signal` | `#4a86ff` | playhead, focus, anything you can act on |
| `--tick` | `#35c9a6` | read receipt green, seen state |

Mention state is `--flare` because that is the one thing that must interrupt.

### Type
- **Bricolage Grotesque** — the machine's voice: date, chat names, counts, buttons, controls.
- **Newsreader** — the prose af_heart speaks: summaries and drafts. Serif because
  this text is read *to* someone; it is content, not chrome.

Two families, two roles. Tabular figures everywhere numbers appear. Mono is not
used for labels.

Scale: 13 / 14.5 / 16 / 19 / 34 with tight negative tracking on the display line.
Body measure capped at ~66 characters.

### Layout
Single 620px column, left-aligned. Chats are **ruled entries**, not cards: a 2px
gutter rule carrying the state (flare = mentioned, signal = unread, dim = seen) and
a hairline between entries. The drafted reply carries a bubble tail pointing left,
because it is his voice pointing back into the thread.

```
 ● Signal Roll-up                  Tonight ▾
 ─────────────────────────────────────────
 Saturday, October 3rd          ← display
 14 chats · 312 messages · mentions in 3

 ▐▛▜▐▛▜▐▛▜▐▛▜▐▛▜▐▛▜▐▛▜▐▛▜▐▛▜   ← the waveform IS the night
 ────────────────●───────────
 ▶ PLAY ALL        1:42/8:20   0.9× 1× 1.15× 1.3×

 ▌ Weekend Builders        12 unread · to 21:14   ▶
 │  @Samuel, Sam T
 │  Dana wants the deck by Friday and Ike asked…
 │  ╭──────────────────────────╮
 │  │ Draft — send this       │╰ bubble tail = his voice
 │  ╰──────────────────────────╯ [Copy]
 ─────────────────────────────────────────
 ▌ Dana Reyes           2 unread · to 19:02    ▶
```

## Signature: the waveform is the interface
Each bar group is one chat; each bar is a real slice of that chat's audio
(≈6 seconds). The playhead sweeps across the night, spoken audio bars fill
`--ash`, the active chat is `--flare` or `--signal`, unspoken is `--rule`.
Tapping a bar jumps to that chat; tapping inside a group seeks inside it.
This encodes time + conversation structure rather than decorating.

## Restraint
- One loud element (the waveform). Everything else quiet and disciplined.
- No gradient blobs, no per-row scroll reveals, no hover transforms on every row.
- Motion only answers an action: play moves, pause stops cold, reduced-motion
  freezes the whole thing and keeps the colour state so nothing is lost.
- Uppercase is not used as decoration; labels are sentence case.
- Links do not wear arrows.

## States, in the interface's voice
- Unlinked: "Not connected to Signal. Scan once to link this device." → /link
- Quiet night: "Nothing came in today. Tomorrow's roll-up is at 8." (empty is an invitation, not a shrug)
- No reply needed: "Nothing here needs you." — not "N/A"
- Voice box down: "af_heart did not answer. Summaries are still here; audio retries at 8 tomorrow."
- Model down: "Summaries fell back to a plain tally." — never a stack trace
