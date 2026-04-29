"""Tiny SQLite-backed task store. Stores submission history + JAV scrape state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from .config import settings

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(settings.state_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    with _lock, _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           TEXT PRIMARY KEY,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                kind         TEXT NOT NULL,
                input_text   TEXT NOT NULL,
                state        TEXT NOT NULL,
                hash         TEXT,
                save_path    TEXT,
                title        TEXT,
                mp_response  TEXT,
                mdcx_result  TEXT,
                error        TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_hash ON tasks(hash)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")

        # Phase 2: discovery caches (TTL-based)
        c.execute("""
            CREATE TABLE IF NOT EXISTS actor_search_cache (
                query       TEXT PRIMARY KEY,
                fetched_at  REAL NOT NULL,
                results_json TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS actor_films_cache (
                actor_id    TEXT PRIMARY KEY,
                fetched_at  REAL NOT NULL,
                films_json  TEXT NOT NULL
            )
        """)
        # Phase 1: per-番号 sukebei search cache
        c.execute("""
            CREATE TABLE IF NOT EXISTS jav_search_cache (
                code            TEXT PRIMARY KEY,
                fetched_at      REAL NOT NULL,
                candidates_json TEXT NOT NULL
            )
        """)


# ---------------------------------------------------------------------------
# Phase 2 cache helpers
# ---------------------------------------------------------------------------

def _ttl_ok(fetched_at: float) -> bool:
    from .config import settings
    return (time.time() - fetched_at) < settings.discover_cache_ttl_sec


def actor_search_cache_get(query: str) -> Optional[list[dict]]:
    with _lock, _db() as c:
        row = c.execute(
            "SELECT fetched_at, results_json FROM actor_search_cache WHERE query = ?",
            (query.lower(),),
        ).fetchone()
    if row and _ttl_ok(row["fetched_at"]):
        return json.loads(row["results_json"])
    return None


def actor_search_cache_set(query: str, results: list[dict]) -> None:
    with _lock, _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO actor_search_cache (query, fetched_at, results_json) VALUES (?, ?, ?)",
            (query.lower(), time.time(), json.dumps(results, ensure_ascii=False)),
        )


def actor_films_cache_get(actor_id: str) -> Optional[list[dict]]:
    with _lock, _db() as c:
        row = c.execute(
            "SELECT fetched_at, films_json FROM actor_films_cache WHERE actor_id = ?",
            (actor_id,),
        ).fetchone()
    if row and _ttl_ok(row["fetched_at"]):
        return json.loads(row["films_json"])
    return None


def actor_films_cache_set(actor_id: str, films: list[dict]) -> None:
    with _lock, _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO actor_films_cache (actor_id, fetched_at, films_json) VALUES (?, ?, ?)",
            (actor_id, time.time(), json.dumps(films, ensure_ascii=False)),
        )


def jav_search_cache_get(code: str) -> Optional[list[dict]]:
    with _lock, _db() as c:
        row = c.execute(
            "SELECT fetched_at, candidates_json FROM jav_search_cache WHERE code = ?",
            (code.upper(),),
        ).fetchone()
    if row and _ttl_ok(row["fetched_at"]):
        return json.loads(row["candidates_json"])
    return None


def jav_search_cache_set(code: str, candidates: list[dict]) -> None:
    with _lock, _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO jav_search_cache (code, fetched_at, candidates_json) VALUES (?, ?, ?)",
            (code.upper(), time.time(), json.dumps(candidates, ensure_ascii=False)),
        )


def add(*, kind: str, input_text: str, state: str, **fields: Any) -> str:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    cols = ["id", "created_at", "updated_at", "kind", "input_text", "state"]
    vals: list[Any] = [tid, now, now, kind, input_text, state]
    for k, v in fields.items():
        cols.append(k)
        vals.append(json.dumps(v) if k in ("mp_response", "mdcx_result") and not isinstance(v, str) else v)
    placeholders = ", ".join("?" * len(cols))
    with _lock, _db() as c:
        c.execute(f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({placeholders})", vals)
    return tid


def update(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        if k in ("mp_response", "mdcx_result") and not isinstance(v, str):
            vals.append(json.dumps(v, ensure_ascii=False))
        else:
            vals.append(v)
    vals.append(task_id)
    with _lock, _db() as c:
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)


def get(task_id: str) -> Optional[dict]:
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def find_by_hash(torrent_hash: str) -> Optional[dict]:
    with _lock, _db() as c:
        row = c.execute(
            "SELECT * FROM tasks WHERE hash = ? ORDER BY created_at DESC LIMIT 1",
            (torrent_hash,),
        ).fetchone()
    return dict(row) if row else None


def list_recent(limit: int = 50) -> list[dict]:
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
