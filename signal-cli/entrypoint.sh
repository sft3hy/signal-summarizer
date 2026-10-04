#!/usr/bin/env bash
# signal-cli daemon, multi-account mode.
#
# No -a account flag: that is what exposes startLink/finishLink over HTTP, so
# the summarizer can drive the QR pairing without the daemon needing an account
# that does not exist yet. Once linked, the account is numberless and is keyed
# by its ACI, which listAccounts reports.
#
# --receive-mode=on-start keeps the long-lived receiving websocket open from
# boot. The Signal protocol expects this: it is how keys rotate, groups update
# and expiry timers work (README: "the Signal protocol expects that incoming
# messages are regularly received").
set -euo pipefail

DATA_DIR="${SIGNAL_DATA_DIR:-/var/lib/signal-cli}"
HTTP_ADDR="${SIGNAL_HTTP_ADDR:-0.0.0.0:8080}"
ARGS=(--data-dir "${DATA_DIR}")

# --verbose writes plaintext message bodies to docker logs. Off by default; the
# daemon's own /api/v1/events stream is the data path, logs stay quiet.
if [ "${SIGNAL_VERBOSE:-0}" = "1" ]; then
  ARGS+=(--verbose)
fi

exec signal-cli "${ARGS[@]}" daemon \
  --http "${HTTP_ADDR}" \
  --receive-mode on-start \
  --ignore-attachments \
  --ignore-avatars \
  --ignore-stickers
# Media never lands on disk. The daemon reports attachment metadata (count,
# content type, filename) in the envelope, so a roll-up can say "3 photos from
# Dana" without the photos themselves ever being stored here.
