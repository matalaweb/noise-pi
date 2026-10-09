# Optional ARM64 container packaging. Same code, lockfile, data layout and acceptance suite as the
# native install. Build on the Pi or with buildx:  docker buildx build --platform linux/arm64 ...
FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends libportaudio2 libsndfile1 alsa-utils tini \
 && rm -rf /var/lib/apt/lists/*

# The container user must be in a group with the *host's* /dev/snd group id (see compose.yaml).
ARG UID=10001
RUN useradd --system --uid ${UID} --home-dir /var/lib/noise-collector --shell /usr/sbin/nologin noise-collector

WORKDIR /opt/noise-collector
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
COPY docs ./docs
RUN python -m pip install --no-cache-dir --no-deps . && noise-collector --version

ENV NOISE_COLLECTOR_CONFIG=/etc/noise-collector/collector.toml \
    NOISE_COLLECTOR_RUN_DIR=/run/noise-collector \
    PYTHONUNBUFFERED=1
USER noise-collector
VOLUME ["/var/lib/noise-collector"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -m noise_collector.healthcheck || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["noise-collector", "run"]
