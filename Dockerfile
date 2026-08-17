FROM python:3.12-slim

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
ENV PODCAST_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# --proxy-headers makes client IPs (used by login throttling) reflect the real
# caller rather than the reverse proxy.
CMD ["uvicorn", "server.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
