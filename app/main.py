"""Archiviatore YouTube & Social - API FastAPI + Web UI."""
from __future__ import annotations

import json
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from . import config, db, files
from .manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("archiviatore")

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init()
    log.info("Download in %s | dati in %s | worker=%d",
             config.DOWNLOAD_DIR, config.DATA_DIR, config.MAX_WORKERS)
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg NON trovato: merge video+audio e conversione MP3 falliranno")
    yield
    manager.shutdown()


app = FastAPI(title=config.APP_NAME, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class DownloadRequest(BaseModel):
    url: str = Field(min_length=5, max_length=2048)
    kind: str = "video"          # video | audio
    playlist: bool = False

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("L'URL deve iniziare con http:// o https://")
        return v

    @field_validator("kind")
    @classmethod
    def check_kind(cls, v: str) -> str:
        if v not in ("video", "audio"):
            raise ValueError("kind deve essere 'video' oppure 'audio'")
        return v


# --- UI -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": config.APP_NAME,
            "max_height": config.MAX_HEIGHT,
            "audio_format": config.AUDIO_FORMAT.upper(),
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "downloads": str(config.DOWNLOAD_DIR)}


# --- Coda / job -----------------------------------------------------------

@app.post("/api/download", status_code=201)
async def start_download(req: DownloadRequest):
    # piu' URL incollati insieme (uno per riga) -> un job ciascuno
    urls = [u.strip() for u in req.url.splitlines() if u.strip()]
    job_ids = [manager.enqueue(u, req.kind, req.playlist) for u in urls]
    return {"job_ids": job_ids, "count": len(job_ids)}


@app.get("/api/jobs")
async def get_jobs(limit: int = Query(100, ge=1, le=500)):
    jobs = db.list_jobs(limit)
    live = manager.live_state()
    for job in jobs:
        state = live.get(job["id"])
        if state and job["status"] in ("queued", "running"):
            job.update({k: v for k, v in state.items() if v not in (None, "")})
    return {"jobs": jobs, "stats": db.stats()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job inesistente")
    job.update(manager.live_state().get(job_id, {}))
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    if not manager.cancel(job_id):
        raise HTTPException(409, "Job gia' concluso o inesistente")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry", status_code=201)
async def retry_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job inesistente")
    new_id = manager.enqueue(job["url"], job["kind"], bool(job["playlist"]))
    return {"job_id": new_id}


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job inesistente")
    if job["status"] in ("queued", "running"):
        manager.cancel(job_id)
    db.delete_job(job_id)
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs():
    return {"removed": db.clear_finished()}


# --- Galleria -------------------------------------------------------------

@app.get("/api/files")
async def list_files(
    q: str = "",
    platform: str = "",
    kind: str = "",
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    items = files.scan()
    platforms = sorted({i["platform"] for i in items})
    if q:
        needle = q.lower()
        items = [i for i in items
                 if needle in i["name"].lower() or needle in i["author"].lower()]
    if platform:
        items = [i for i in items if i["platform"] == platform]
    if kind:
        items = [i for i in items if i["kind"] == kind]
    total = len(items)
    return {
        "items": items[offset:offset + limit],
        "total": total,
        "platforms": platforms,
        "bytes": sum(i["size"] for i in items),
    }


@app.get("/api/files/stream")
async def stream_file(request: Request, path: str):
    return files.media_response(files.safe_path(path), request, download=False)


@app.get("/api/files/download")
async def download_file(request: Request, path: str):
    return files.media_response(files.safe_path(path), request, download=True)


@app.get("/api/files/metadata")
async def file_metadata(path: str):
    media = files.safe_path(path)
    meta = media.parent / (media.name + ".metadata.json")
    if not meta.is_file():
        raise HTTPException(404, "Nessun metadato per questo file")
    return JSONResponse(content=json.loads(meta.read_text(encoding="utf-8")))


@app.delete("/api/files")
async def delete_file(path: str):
    removed = files.delete(path)
    return {"ok": True, "removed": removed}
