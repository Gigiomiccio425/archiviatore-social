"""Galleria: scansione della cartella download, anteprime e streaming con Range."""
from __future__ import annotations

import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import config

_CACHE: dict[str, object] = {"at": 0.0, "items": []}
_CACHE_TTL = 5.0  # secondi
CHUNK = 1024 * 512

# mimetypes dipende da /etc/mime.types, che nell'immagine slim non c'e': senza
# questa tabella .mp4 e .m4a escono come application/octet-stream e il browser
# rifiuta di riprodurli mostrando il play sbarrato.
MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Contenitori che nessun browser sa aprire: la UI deve dirlo invece di
# mostrare un player morto.
UNPLAYABLE_EXT = {".mkv", ".avi", ".flv", ".ts"}


def media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MIME_TYPES:
        return MIME_TYPES[suffix]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def safe_path(rel_path: str) -> Path:
    """Risolve un percorso relativo impedendo qualsiasi uscita da DOWNLOAD_DIR."""
    if not rel_path:
        raise HTTPException(400, "Percorso mancante")
    candidate = (config.DOWNLOAD_DIR / rel_path.lstrip("/\\")).resolve()
    if not candidate.is_relative_to(config.DOWNLOAD_DIR):
        raise HTTPException(403, "Percorso non consentito")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(404, "File non trovato")
    return candidate


def _kind(suffix: str) -> str:
    if suffix in config.VIDEO_EXT:
        return "video"
    if suffix in config.AUDIO_EXT:
        return "audio"
    return "altro"


def scan(force: bool = False) -> list[dict]:
    """Elenca i media presenti su disco. Cache breve: la cartella puo' essere grande."""
    now = time.time()
    if not force and now - float(_CACHE["at"]) < _CACHE_TTL:
        return _CACHE["items"]  # type: ignore[return-value]

    items: list[dict] = []
    root = config.DOWNLOAD_DIR
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        images = {
            Path(f).stem: f for f in filenames if Path(f).suffix.lower() in config.IMAGE_EXT
        }
        for fname in filenames:
            path = Path(dirpath) / fname
            suffix = path.suffix.lower()
            if suffix not in config.MEDIA_EXT:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(root)
            parts = rel.parts
            thumb_name = images.get(path.stem)
            items.append({
                "rel_path": str(rel).replace("\\", "/"),
                "name": path.stem,
                "ext": suffix.lstrip("."),
                "kind": _kind(suffix),
                "playable": suffix not in UNPLAYABLE_EXT,
                "platform": parts[0] if len(parts) > 1 else "Altro",
                "author": parts[1] if len(parts) > 2 else "",
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "thumb": (
                    str((Path(dirpath) / thumb_name).relative_to(root)).replace("\\", "/")
                    if thumb_name else None
                ),
            })

    items.sort(key=lambda i: i["mtime"], reverse=True)
    _CACHE["at"] = now
    _CACHE["items"] = items
    return items


def invalidate() -> None:
    _CACHE["at"] = 0.0


def delete(rel_path: str) -> list[str]:
    """Cancella il media piu' i suoi file satellite (copertina, metadati, sottotitoli)."""
    target = safe_path(rel_path)
    stem = target.stem
    sidecar_suffixes = (".metadata.json", ".info.json", ".description")
    removed = []
    for sibling in list(target.parent.iterdir()):
        if not sibling.is_file():
            continue
        is_target = sibling == target
        # copertina: stesso stem, estensione immagine  -> "video.webp"
        is_thumb = sibling.stem == stem and sibling.suffix.lower() in config.IMAGE_EXT
        # satelliti: nome completo + suffisso  -> "video.mp4.metadata.json"
        is_sidecar = sibling.name.startswith(target.name + ".") and \
            sibling.name.endswith(sidecar_suffixes)
        # sottotitoli: stem + lingua  -> "video.it.vtt"
        is_sub = sibling.suffix.lower() in (".vtt", ".srt") and \
            sibling.stem.startswith(stem + ".")
        if is_target or is_thumb or is_sidecar or is_sub:
            try:
                sibling.unlink()
                removed.append(sibling.name)
            except OSError:
                pass
    # rimuove le cartelle rimaste vuote fino alla radice
    parent = target.parent
    while parent != config.DOWNLOAD_DIR and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    invalidate()
    return removed


def _iter_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            data = fh.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def media_response(path: Path, request: Request, download: bool = False) -> object:
    """FileResponse per il download, risposta 206 con Range per il player."""
    media_type = media_type_for(path)
    if download:
        return FileResponse(path, media_type=media_type, filename=path.name)

    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    # HEAD: il player chiede solo gli header, inutile leggere il file
    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    if not range_header:
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Accept-Ranges": "bytes", "Content-Length": str(file_size)},
        )

    m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not m:
        raise HTTPException(416, "Range non valido")
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end:
        raise HTTPException(416, "Range fuori dal file")

    return StreamingResponse(
        _iter_range(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        },
    )
