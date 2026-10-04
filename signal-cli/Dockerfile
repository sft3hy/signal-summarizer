# signal-cli 0.14.8 — arm64 aware.
#
# Upstream note (README, v0.14.8): "native libs are bundled for x86_64 Linux
# (with recent enough glibc), Windows and MacOS" — NOT arm64 Linux. This Studio
# runs OrbStack (linux/arm64), so libsignal_jni.so is injected from
# exquo/signal-libs-build, which compiles upstream libsignal on GitHub Actions
# and publishes a sha256 in the workflow log. The version is read out of the
# jar name so the pair can never drift.
#
# Same trick ~/dev/signal-cs used for 0.14.5, pinned forward to 0.14.8.

FROM eclipse-temurin:25-jre-noble

# signal-cli README: "needs to be kept up-to-date... releases older than three
# months may not work correctly." Bump this with the dashboard card in view.
ARG SIGNAL_CLI_VERSION=0.14.8
# sha256 of signal-cli-0.14.8.tar.gz from the GitHub release asset digest.
ARG SIGNAL_CLI_SHA256=ccd408e831eff7e41ebaaf309704840bb00d78a7869f35ad700dbae5b5a5bb65

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/signal-cli.tar.gz \
        "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
    && echo "${SIGNAL_CLI_SHA256}  /tmp/signal-cli.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/signal-cli.tar.gz -C /opt \
    && rm /tmp/signal-cli.tar.gz \
    && ln -sfn "/opt/signal-cli-${SIGNAL_CLI_VERSION}" /opt/signal-cli

# Resolve the bundled libsignal version from the jar name, then place an
# arm64-built libsignal_jni.so next to it and point java.library.path at the
# directory so the JVM loads the .so instead of the x86_64 copy inside the jar.
RUN set -eux; \
    arch="$(uname -m)"; \
    jar="$(ls /opt/signal-cli/lib/libsignal-client-*.jar)"; \
    ver="$(basename "${jar}" .jar | sed 's/^libsignal-client-//')"; \
    echo "signal-cli ${SIGNAL_CLI_VERSION} -> libsignal ${ver} on ${arch}"; \
    if [ "${arch}" = "aarch64" ]; then \
        curl -fsSL "https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${ver}/libsignal_jni.so-v${ver}-aarch64-unknown-linux-gnu.tar.gz" \
            | tar -xz -C /opt/signal-cli/lib; \
    fi; \
    ls -l /opt/signal-cli/lib/libsignal_jni*.so

ENV JAVA_TOOL_OPTIONS="-Djava.library.path=/opt/signal-cli/lib" \
    PATH="/opt/signal-cli/bin:${PATH}" \
    XDG_DATA_HOME=/var/lib

RUN useradd --system --create-home --shell /usr/sbin/nologin signal \
    && mkdir -p /var/lib/signal-cli \
    && chown -R signal:signal /var/lib/signal-cli /opt/signal-cli-${SIGNAL_CLI_VERSION}

COPY entrypoint.sh /usr/local/bin/signal-daemon-entrypoint
RUN chmod +x /usr/local/bin/signal-daemon-entrypoint

USER signal
WORKDIR /var/lib

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/api/v1/check || exit 1

ENTRYPOINT ["/usr/local/bin/signal-daemon-entrypoint"]
