#!/bin/sh
# One container, two processes, no root workload.
#
#   cron   runs as root because vixie-cron only reads /etc/cron.d as root. Every
#          entry in that file names `summarizer`, so nothing it runs is privileged.
#   server demoted to `summarizer` via setpriv before exec, so the process holding
#          your chat summaries on the tailnet is unprivileged from birth.
#
# CMD:
#   serve       cron + web app (default)
#   rollup      one roll-up and exit (what cron calls, and what you can run by hand)
#   check       diagnostics: signal-cli, voice, omlx, sqlite
set -eu

DATA="${SS_DATA_DIR:-/data}"
mkdir -p "$DATA/logs" "$DATA/audio" 2>/dev/null || true
# Self-heal ownership. `docker compose exec` runs as root by default, and a
# roll-up run that way creates /data/audio/<day>/ as root:root — after which the
# real server process (demoted to `summarizer` below) cannot write a single clip
# and silently ships a night with no audio. Cheap insurance at every boot.
chown -R summarizer:summarizer "$DATA" 2>/dev/null || true

drop() {
  if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid=summarizer --regid=summarizer --clear-groups "$@"
  fi
  # No setpriv: run as-is rather than dying. Loud about it.
  echo "WARN: setpriv missing, running as $(id -un)" >&2
  exec "$@"
}

case "${1:-serve}" in
  rollup)
    shift 2>/dev/null || true
    drop python3 /app/bin/rollup "$@"
    ;;
  check)
    shift 2>/dev/null || true
    drop python3 /app/bin/check "$@"
    ;;
  serve)
    /usr/sbin/cron
    echo "cron: $(grep -v '^#' /etc/cron.d/rollup | head -1)"
    drop python3 -m app.server
    ;;
  *)
    exec "$@"
    ;;
esac
