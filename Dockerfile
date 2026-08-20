FROM python:3.12-slim

# Set via --build-arg from the version tag the image is published under (see
# docker-publish.yml), so the running app can show what it actually is.
ARG VERSION=dev
ENV PODCAST_VERSION=$VERSION

# ffmpeg/ffprobe do the format normalisation that keeps Garmin's media pipeline
# happy; the app refuses to download without them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/

# Runs unprivileged. For a bind mount the host directory's ownership wins, so
# the TrueNAS dataset needs `chown -R 1000:1000` (see README).
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /data && chown appuser:appuser /data
USER appuser

VOLUME ["/data"]

# Every setting has a usable default, so the container runs with no environment
# configuration at all. Override any of them at deploy time.
#
# PODCAST_SECRET_KEY is deliberately absent: it is generated on first boot and
# persisted to /data/secret.key, so sessions survive restarts on their own.
ENV PODCAST_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PODCAST_ADMIN_USER=admin \
    PODCAST_ADMIN_PASSWORD=changeme \
    PODCAST_BASE_URL="" \
    PODCAST_COOKIE_SECURE=false \
    PODCAST_RETENTION_DAYS=14 \
    PODCAST_EPISODES_PER_FEED=5 \
    PODCAST_REFRESH_MINUTES=15 \
    PODCAST_TRANSCODE_MODE=auto \
    PODCAST_MAX_BITRATE_KBPS=128 \
    PODCAST_TARGET_BITRATE_KBPS=64 \
    PODCAST_LOG_LEVEL=INFO

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# --proxy-headers makes client IPs (used by login throttling) reflect the real
# caller rather than the reverse proxy.
CMD ["uvicorn", "server.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
