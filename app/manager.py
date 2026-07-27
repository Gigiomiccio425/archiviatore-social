"""Motore di download: coda a thread + yt-dlp.

Ogni job gira in un thread del pool (MAX_WORKERS), quindi le route FastAPI
restano libere. Lo stato istantaneo (percentuale, velocita', ETA) vive in
memoria e viene scritto su SQLite a intervalli, per non martellare il disco.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yt_dlp

from . import config, db

log = logging.getLogger("archiviatore.manager")

try:  # presente in yt-dlp recenti: interrompe anche le playlist
    from yt_dlp.utils import DownloadCancelled
except ImportError:  # pragma: no cover
    class DownloadCancelled(Exception):
        pass


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DB_FLUSH_EVERY = 1.5  # secondi fra due scritture di avanzamento


def human_size(num: float | int | None) -> str:
    if not num:
        return ""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} PB"


def human_eta(seconds: Any) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def platform_label(extractor_key: str | None, extractor: str | None) -> str:
    key = (extractor or extractor_key or "generic").lower().split(":")[0]
    return config.PLATFORM_ALIASES.get(key, (extractor_key or "Altro").split(":")[0])


class DownloadManager:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=config.MAX_WORKERS, thread_name_prefix="dl"
        )
        self._lock = threading.Lock()
        self._live: dict[str, dict] = {}      # job_id -> avanzamento istantaneo
        self._cancelled: set[str] = set()
        self._last_flush: dict[str, float] = {}

    # --- API pubblica -----------------------------------------------------

    def enqueue(self, url: str, kind: str = "video", playlist: bool = False) -> str:
        job_id = uuid.uuid4().hex[:12]
        db.create_job(job_id, url, kind, playlist)
        with self._lock:
            self._live[job_id] = {"status": "queued", "progress": 0.0}
        self._pool.submit(self._run, job_id, url, kind, playlist)
        return job_id

    def cancel(self, job_id: str) -> bool:
        job = db.get_job(job_id)
        if not job or job["status"] in ("done", "error", "canceled"):
            return False
        with self._lock:
            self._cancelled.add(job_id)
        # Se era ancora in coda il thread non e' mai partito: chiudilo subito.
        if job["status"] == "queued":
            db.update_job(job_id, status="canceled", phase="Annullato")
        return True

    def live_state(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._live.items()}

    def shutdown(self) -> None:
        with self._lock:
            self._cancelled.update(self._live.keys())
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- interno ----------------------------------------------------------

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def _set_live(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            self._live.setdefault(job_id, {}).update(fields)

    def _flush(self, job_id: str, force: bool = False, **fields: Any) -> None:
        """Scrive su DB al massimo ogni DB_FLUSH_EVERY secondi."""
        now = time.time()
        if not force and now - self._last_flush.get(job_id, 0) < DB_FLUSH_EVERY:
            return
        self._last_flush[job_id] = now
        db.update_job(job_id, **fields)

    def _run(self, job_id: str, url: str, kind: str, playlist: bool) -> None:
        if self._is_cancelled(job_id):
            db.update_job(job_id, status="canceled", phase="Annullato")
            self._set_live(job_id, status="canceled")
            return

        db.update_job(job_id, status="running", phase="Analisi del link...")
        self._set_live(job_id, status="running", progress=0.0, phase="Analisi del link...")

        try:
            meta = self._probe(url, playlist)
            db.update_job(
                job_id,
                title=meta["title"],
                platform=meta["platform"],
                uploader=meta["uploader"],
                thumbnail=meta["thumbnail"],
                items_total=meta["count"],
                phase="Download in corso",
            )
            self._set_live(job_id, phase="Download in corso", **{
                "title": meta["title"], "items_total": meta["count"]
            })

            results = self._download(job_id, url, kind, meta)

            if self._is_cancelled(job_id):
                raise DownloadCancelled()

            if not results:
                raise RuntimeError("Nessun file prodotto (contenuto non disponibile?)")

            for item in results:
                db.add_job_file(job_id, item["rel_path"], item["title"], item["size"])

            db.update_job(
                job_id,
                status="done",
                progress=100.0,
                phase="Completato",
                items_done=len(results),
                speed=None,
                eta=None,
                error=None,
            )
            self._set_live(job_id, status="done", progress=100.0, phase="Completato")

        except DownloadCancelled:
            db.update_job(job_id, status="canceled", phase="Annullato", speed=None, eta=None)
            self._set_live(job_id, status="canceled", phase="Annullato")
        except Exception as exc:  # noqa: BLE001 - qualunque errore finisce nello storico
            message = ANSI_RE.sub("", str(exc)).strip() or exc.__class__.__name__
            log.exception("Job %s fallito", job_id)
            db.update_job(job_id, status="error", error=message[:900],
                          phase="Errore", speed=None, eta=None)
            self._set_live(job_id, status="error", phase="Errore", error=message[:300])
        finally:
            with self._lock:
                self._cancelled.discard(job_id)
                self._last_flush.pop(job_id, None)
            # lascia l'ultimo stato visibile qualche istante, poi libera memoria
            cleaner = threading.Timer(30, lambda: self._live.pop(job_id, None))
            cleaner.daemon = True
            cleaner.start()

    def _probe(self, url: str, playlist: bool) -> dict:
        """Legge i metadati senza scaricare: serve a costruire i percorsi puliti."""
        opts = self._base_opts()
        opts.update({
            "skip_download": True,
            "noplaylist": not playlist,
            # flat: per una playlist lunga non risolviamo i formati di ogni video
            "extract_flat": "in_playlist" if playlist else False,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = info.get("entries")
        is_playlist = bool(entries) and info.get("_type") in ("playlist", "multi_video")
        first: dict = {}
        count = 1
        if is_playlist:
            # entries puo' essere una LazyList: prendiamo un solo elemento,
            # senza forzare la risoluzione dell'intera playlist.
            for entry in entries:
                if entry:
                    first = entry
                    break
            count = info.get("playlist_count") or 0
            if not count:
                try:
                    count = len(entries)
                except TypeError:
                    count = 1
        else:
            first = info

        uploader = (
            info.get("uploader") or info.get("channel") or info.get("creator")
            or first.get("uploader") or first.get("channel") or first.get("uploader_id")
            or "Sconosciuto"
        )
        return {
            "is_playlist": is_playlist,
            "count": count,
            "title": info.get("title") or first.get("title") or url,
            "uploader": uploader,
            "platform": platform_label(
                info.get("extractor_key") or first.get("extractor_key"),
                info.get("extractor") or first.get("extractor"),
            ),
            "thumbnail": info.get("thumbnail") or first.get("thumbnail"),
        }

    def _base_opts(self) -> dict:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            "ignoreerrors": False,
            "retries": 5,
            "fragment_retries": 10,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "restrictfilenames": False,
            "windowsfilenames": config.WINDOWS_SAFE_NAMES,
            "trim_file_name": config.TRIM_FILE_NAME,
            "http_headers": {"User-Agent": "Mozilla/5.0"},
        }
        if config.COOKIES_FILE and Path(config.COOKIES_FILE).is_file():
            opts["cookiefile"] = config.COOKIES_FILE
        if config.RATE_LIMIT:
            opts["ratelimit"] = self._parse_rate(config.RATE_LIMIT)
        return opts

    @staticmethod
    def _parse_rate(value: str) -> int | None:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)B?", value.strip(), re.I)
        if not m:
            return None
        mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]
        return int(float(m.group(1)) * mult)

    def _outtmpl(self, meta: dict, playlist: bool) -> str:
        platform = re.sub(r"[^\w .()\-]", "_", meta["platform"]) or "Altro"
        author = "%(uploader,channel,creator,artist,uploader_id|Sconosciuto)s"
        name = "%(title)s"
        if config.INCLUDE_VIDEO_ID:
            name += " [%(id)s]"
        if playlist and meta["is_playlist"]:
            return (
                f"{platform}/{author}/%(playlist_title,playlist|Playlist)s/"
                f"%(playlist_index)03d - {name}.%(ext)s"
            )
        return f"{platform}/{author}/{name}.%(ext)s"

    def _format_selector(self, kind: str) -> str:
        if kind == "audio":
            return "bestaudio/best"
        if config.MAX_HEIGHT > 0:
            h = config.MAX_HEIGHT
            return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        return "bv*+ba/b"

    def _download(self, job_id: str, url: str, kind: str, meta: dict) -> list[dict]:
        playlist = meta["is_playlist"]
        opts = self._base_opts()
        opts.update({
            "paths": {
                "home": str(config.DOWNLOAD_DIR),
                "temp": str(config.TEMP_DIR),
            },
            "outtmpl": {"default": self._outtmpl(meta, playlist)},
            "format": self._format_selector(kind),
            "noplaylist": not playlist,
            "ignoreerrors": "only_download" if playlist else False,
            "continuedl": True,
            "nooverwrites": config.SKIP_EXISTING,
            "writethumbnail": config.WRITE_THUMBNAIL,
            "progress_hooks": [lambda d: self._on_progress(job_id, d)],
            "postprocessor_hooks": [lambda d: self._on_postprocess(job_id, d)],
            "postprocessors": [],
        })

        if kind == "audio":
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": config.AUDIO_FORMAT,
                    "preferredquality": config.AUDIO_QUALITY,
                },
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]
            if config.WRITE_THUMBNAIL:
                # already_have_thumbnail=True -> la copertina resta anche su disco
                opts["postprocessors"].append(
                    {"key": "EmbedThumbnail", "already_have_thumbnail": True}
                )
        else:
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]
            if config.PREFER_MP4:
                # h264+aac in mp4: si apre nel player del browser e su iOS
                opts["format_sort"] = ["res", "ext:mp4:m4a", "vcodec:h264", "acodec:aac"]
                # merge_output_format da solo non basta: se i flussi scelti non
                # sono compatibili con mp4, yt-dlp ripiega su mkv e il browser
                # non lo apre. Il remux corregge il contenitore senza ricodifica.
                opts["postprocessors"].insert(
                    0, {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
                )
            if config.EMBED_SUBTITLES:
                opts.update({
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": config.SUBTITLE_LANGS.split(","),
                })
                opts["postprocessors"].insert(
                    0, {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}
                )

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        entries = info.get("entries") if isinstance(info, dict) else None
        items = [e for e in (entries if entries is not None else [info]) if e]

        results: list[dict] = []
        for entry in items:
            path = self._final_path(entry)
            if not path or not path.exists():
                continue
            if config.WRITE_METADATA_FILE:
                self._write_metadata(path, entry)
            results.append({
                "rel_path": str(path.relative_to(config.DOWNLOAD_DIR)).replace("\\", "/"),
                "title": entry.get("title") or path.stem,
                "size": path.stat().st_size,
            })
        return results

    @staticmethod
    def _final_path(entry: dict) -> Path | None:
        """Percorso del file DOPO merge/conversione."""
        for req in entry.get("requested_downloads") or []:
            fp = req.get("filepath") or req.get("_filename")
            if fp:
                return Path(fp)
        fp = entry.get("filepath") or entry.get("_filename")
        return Path(fp) if fp else None

    @staticmethod
    def _write_metadata(media: Path, entry: dict) -> None:
        payload = {
            "titolo": entry.get("title"),
            "autore": entry.get("uploader") or entry.get("channel"),
            "canale_url": entry.get("uploader_url") or entry.get("channel_url"),
            "piattaforma": platform_label(entry.get("extractor_key"), entry.get("extractor")),
            "url_originale": entry.get("webpage_url") or entry.get("original_url"),
            "id": entry.get("id"),
            "data_pubblicazione": entry.get("upload_date"),
            "durata_secondi": entry.get("duration"),
            "visualizzazioni": entry.get("view_count"),
            "like": entry.get("like_count"),
            "descrizione": entry.get("description"),
            "tag": entry.get("tags"),
            "risoluzione": entry.get("resolution"),
            "fps": entry.get("fps"),
            "codec_video": entry.get("vcodec"),
            "codec_audio": entry.get("acodec"),
            "file": media.name,
            "scaricato_il": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            sidecar = media.parent / (media.name + ".metadata.json")
            sidecar.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # non deve mai far fallire il download
            log.warning("Metadati non scritti per %s: %s", media, exc)

    def _on_progress(self, job_id: str, d: dict) -> None:
        if self._is_cancelled(job_id):
            raise DownloadCancelled("Annullato dall'utente")

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            info = d.get("info_dict") or {}
            idx = info.get("playlist_index") or 0
            live = {
                "status": "running",
                "progress": round(pct, 1),
                "speed": human_size(d.get("speed")) + "/s" if d.get("speed") else "",
                "eta": human_eta(d.get("eta")),
                "downloaded": human_size(done),
                "total": human_size(total),
                "phase": f"Download {idx}" if idx else "Download in corso",
                "current": info.get("title"),
            }
            self._set_live(job_id, **live)
            self._flush(
                job_id,
                progress=live["progress"],
                speed=live["speed"],
                eta=live["eta"],
                phase=live["phase"],
                items_done=max(0, (idx or 1) - 1),
            )
        elif status == "finished":
            self._set_live(job_id, progress=100.0, phase="Elaborazione file...", speed="", eta="")
            self._flush(job_id, force=True, progress=100.0, phase="Elaborazione file...")

    def _on_postprocess(self, job_id: str, d: dict) -> None:
        if self._is_cancelled(job_id):
            raise DownloadCancelled("Annullato dall'utente")
        if d.get("status") != "started":
            return
        labels = {
            "FFmpegExtractAudio": "Conversione audio...",
            "FFmpegMerger": "Unione audio + video...",
            "FFmpegVideoConvertor": "Conversione video...",
            "EmbedThumbnail": "Copertina...",
            "FFmpegMetadata": "Scrittura metadati...",
            "FFmpegEmbedSubtitle": "Sottotitoli...",
            "MoveFiles": "Spostamento file...",
        }
        phase = labels.get(d.get("postprocessor", ""), "Elaborazione...")
        self._set_live(job_id, phase=phase)
        self._flush(job_id, force=True, phase=phase)


manager = DownloadManager()
