FROM python:3.12-slim

LABEL org.opencontainers.image.title="Archiviatore YouTube & Social" \
      org.opencontainers.image.description="Web UI per scaricare e archiviare video/audio dai social con yt-dlp" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DOWNLOAD_DIR=/downloads \
    DATA_DIR=/data \
    TZ=Europe/Rome

# ffmpeg: obbligatorio per unire video+audio e per la conversione MP3.
# Gli altri pacchetti servono a yt-dlp per HTTPS e per i siti che usano JS/cifratura.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/downloads", "/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
