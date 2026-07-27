#!/bin/sh
set -e

mkdir -p "${DOWNLOAD_DIR:-/downloads}" "${DATA_DIR:-/data}"

# yt-dlp si rompe spesso quando i siti cambiano: con UPDATE_YTDLP=true
# aggiorna la libreria a ogni avvio del container (serve rete in uscita).
if [ "${UPDATE_YTDLP}" = "true" ] || [ "${UPDATE_YTDLP}" = "1" ]; then
    echo "[entrypoint] aggiornamento yt-dlp..."
    pip install --no-cache-dir --upgrade yt-dlp || echo "[entrypoint] aggiornamento fallito, uso la versione presente"
fi

python -c "import yt_dlp, sys; print('[entrypoint] yt-dlp', yt_dlp.version.__version__)"
ffmpeg -version | head -n 1 | sed 's/^/[entrypoint] /'

exec "$@"
