FROM python:3.12-slim

# cron because "once a day at 8pm" should be a crontab entry, not a sleep loop.
# tzdata because 20:00 has to mean 20:00 in America/Los_Angeles, not UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/America/Los_Angeles /etc/localtime \
    && echo "America/Los_Angeles" > /etc/timezone

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Los_Angeles \
    SS_DATA_DIR=/data \
    SS_WEB_PORT=8080 \
    SS_SIGNAL_RPC_URL=http://signal-cli:8080 \
    SS_TTS_URL=http://host.docker.internal:8001

WORKDIR /app

# No pip install step: the app is stdlib only, so there is no dependency tree to rot.
COPY app /app/app
COPY public /app/public
COPY bin /app/bin
COPY cron/rollup.cron /etc/cron.d/rollup
COPY docker-entrypoint.sh /usr/local/bin/summarizer-entrypoint

RUN useradd --system --create-home --home-dir /home/summarizer --shell /bin/sh summarizer \
    && mkdir -p /data/logs /data/audio \
    && chown -R summarizer:summarizer /data /home/summarizer \
    && chmod +x /usr/local/bin/summarizer-entrypoint /app/bin/rollup

# Stays root on purpose: vixie-cron only honours /etc/cron.d as root, and the
# crontab drops to the unprivileged `summarizer` user in its user field. The web
# app is demoted the same way by the entrypoint. No process runs as root.
RUN chmod 600 /etc/cron.d/rollup && chown root:root /etc/cron.d/rollup

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=8s --start-period=35s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/local/bin/summarizer-entrypoint"]
CMD ["serve"]
