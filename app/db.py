"""Persistenza SQLite dello storico download.

Una sola connessione condivisa (check_same_thread=False) protetta da un lock:
il carico e' minimo e cosi' evitiamo pool/ORM inutili.
"""
import sqlite3
import threading
import time
from typing import Any, Iterable

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'video',   -- video | audio
    playlist     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error|canceled
    title        TEXT,
    platform     TEXT,
    uploader     TEXT,
    thumbnail    TEXT,
    progress     REAL NOT NULL DEFAULT 0,
    speed        TEXT,
    eta          TEXT,
    phase        TEXT,
    items_done   INTEGER NOT NULL DEFAULT 0,
    items_total  INTEGER NOT NULL DEFAULT 1,
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job_files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    rel_path  TEXT NOT NULL,
    title     TEXT,
    size      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_job ON job_files(job_id);
"""


def init() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            return
        config.ensure_dirs()
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        # Un job "running" al riavvio del container e' orfano: lo marchiamo errore.
        _conn.execute(
            "UPDATE jobs SET status='error', error='Interrotto dal riavvio del servizio', "
            "updated_at=? WHERE status IN ('running','queued')",
            (time.time(),),
        )
        _conn.commit()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, tuple(params))
        _conn.commit()
        return cur


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        rows = _conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# --- API di dominio -------------------------------------------------------

def create_job(job_id: str, url: str, kind: str, playlist: bool) -> None:
    now = time.time()
    execute(
        "INSERT INTO jobs (id, url, kind, playlist, status, created_at, updated_at) "
        "VALUES (?,?,?,?, 'queued', ?, ?)",
        (job_id, url, kind, int(playlist), now, now),
    )


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))


def add_job_file(job_id: str, rel_path: str, title: str, size: int) -> None:
    execute(
        "INSERT INTO job_files (job_id, rel_path, title, size, created_at) VALUES (?,?,?,?,?)",
        (job_id, rel_path, title, size, time.time()),
    )


def list_jobs(limit: int = 100) -> list[dict]:
    jobs = query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    if not jobs:
        return []
    placeholders = ",".join("?" * len(jobs))
    files = query(
        f"SELECT job_id, rel_path, title, size FROM job_files WHERE job_id IN ({placeholders})",
        [j["id"] for j in jobs],
    )
    by_job: dict[str, list[dict]] = {}
    for f in files:
        by_job.setdefault(f["job_id"], []).append(f)
    for j in jobs:
        j["files"] = by_job.get(j["id"], [])
    return jobs


def get_job(job_id: str) -> dict | None:
    job = query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if job:
        job["files"] = query(
            "SELECT rel_path, title, size FROM job_files WHERE job_id=?", (job_id,)
        )
    return job


def delete_job(job_id: str) -> None:
    execute("DELETE FROM job_files WHERE job_id=?", (job_id,))
    execute("DELETE FROM jobs WHERE id=?", (job_id,))


def clear_finished() -> int:
    cur = execute("DELETE FROM jobs WHERE status IN ('done','error','canceled')")
    return cur.rowcount


def stats() -> dict:
    row = query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(status='done') AS done, "
        "SUM(status='error') AS errors, "
        "SUM(status IN ('queued','running')) AS active FROM jobs"
    ) or {}
    size = query_one("SELECT COALESCE(SUM(size),0) AS bytes FROM job_files") or {"bytes": 0}
    return {
        "total": row.get("total") or 0,
        "done": row.get("done") or 0,
        "errors": row.get("errors") or 0,
        "active": row.get("active") or 0,
        "bytes": size["bytes"],
    }
