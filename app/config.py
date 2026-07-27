"""Configurazione centralizzata, tutta pilotabile da variabili d'ambiente."""
import os
from pathlib import Path

try:  # comodita' per lo sviluppo locale; in Docker le variabili arrivano dal compose
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on", "si")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


APP_NAME = os.getenv("APP_NAME", "Archiviatore YouTube & Social")

# Cartelle: /downloads = media finali, /data = database + cookie + file temporanei
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads")).expanduser().resolve()
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).expanduser().resolve()
DB_PATH = DATA_DIR / "archive.db"
# I file .part stanno sullo STESSO volume dei media finali: cosi' il file
# completato viene spostato con un rename atomico invece che ricopiato.
TEMP_DIR = DOWNLOAD_DIR / ".incomplete"

# Quanti download in parallelo (il resto resta "in coda")
MAX_WORKERS = _int("MAX_WORKERS", 2)

# 0 = nessun limite (massima risoluzione disponibile). Altrimenti es. 1080, 2160.
MAX_HEIGHT = _int("MAX_HEIGHT", 0)

# Preferisci contenitore mp4/h264: file riproducibili ovunque (Safari/iOS inclusi).
# Metti false se vuoi davvero il bitstream migliore (spesso VP9/AV1 in webm).
PREFER_MP4 = _bool("PREFER_MP4", True)

AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "mp3")
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "192")

# Scrive un <nome>.metadata.json accanto a ogni media
WRITE_METADATA_FILE = _bool("WRITE_METADATA_FILE", True)
# Salva la copertina come file separato (usata come anteprima in Galleria)
WRITE_THUMBNAIL = _bool("WRITE_THUMBNAIL", True)
# Sottotitoli automatici incorporati (solo video)
EMBED_SUBTITLES = _bool("EMBED_SUBTITLES", False)
SUBTITLE_LANGS = os.getenv("SUBTITLE_LANGS", "it,en")

# Nomi file compatibili anche con condivisioni SMB verso Windows
WINDOWS_SAFE_NAMES = _bool("WINDOWS_SAFE_NAMES", True)
TRIM_FILE_NAME = _int("TRIM_FILE_NAME", 120)
# Aggiunge " [id]" al nome file: brutto ma azzera le collisioni fra titoli uguali
INCLUDE_VIDEO_ID = _bool("INCLUDE_VIDEO_ID", False)
# Se un file con lo stesso nome esiste gia', salta invece di riscaricare
SKIP_EXISTING = _bool("SKIP_EXISTING", True)

# Per contenuti privati / rate-limit: esporta i cookie del browser in Netscape format
# e monta il file, es. COOKIES_FILE=/data/cookies.txt
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

# Limite banda per download, es. "5M". Vuoto = illimitato.
RATE_LIMIT = os.getenv("RATE_LIMIT", "").strip()

# Estensioni riconosciute dalla Galleria
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts"}
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT

# Etichette pulite per la cartella di primo livello
PLATFORM_ALIASES = {
    "youtube": "YouTube",
    "youtube:tab": "YouTube",
    "youtube:shorts": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "twitter": "Twitter",
    "x": "Twitter",
    "facebook": "Facebook",
    "reddit": "Reddit",
    "twitch": "Twitch",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "soundcloud": "SoundCloud",
    "generic": "Altro",
}


def ensure_dirs() -> None:
    for path in (DOWNLOAD_DIR, DATA_DIR, TEMP_DIR):
        path.mkdir(parents=True, exist_ok=True)
