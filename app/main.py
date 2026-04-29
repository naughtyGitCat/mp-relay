"""FastAPI entrypoint: web UI + submit endpoint + background watcher."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import store
from .classifier import classify
from .config import settings
from .mdcx_runner import healthcheck as mdcx_healthcheck
from .mp_client import MpClient
from .qbt_client import QbtClient
from .watcher import watch_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("mp-relay")


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _format_ts(epoch: int | float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(int(epoch)).strftime("%m-%d %H:%M")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    log.info("DB initialised at %s", settings.state_db)

    # Ensure qBT JAV category exists
    qbt = QbtClient()
    try:
        await qbt.ensure_category(settings.qbt_jav_category, settings.qbt_jav_savepath)
    except Exception as e:
        log.warning("could not ensure qBT category (qBT might be offline): %s", e)
    finally:
        await qbt.close()

    err = await mdcx_healthcheck()
    if err:
        log.warning("MDCX healthcheck: %s (scraping will fail until fixed)", err)
    else:
        log.info("MDCX healthcheck: OK")

    stop_event = asyncio.Event()
    watcher_task = asyncio.create_task(watch_loop(stop_event), name="watcher")

    try:
        yield
    finally:
        log.info("shutting down")
        stop_event.set()
        try:
            await asyncio.wait_for(watcher_task, timeout=10)
        except asyncio.TimeoutError:
            watcher_task.cancel()


app = FastAPI(title="mp-relay", lifespan=lifespan)


# ============================================================
# UI
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tasks = store.list_recent(limit=50)
    # Pre-format timestamps Python-side; avoids Jinja2 env-filter cache quirks.
    for t in tasks:
        t["created_ts_fmt"] = _format_ts(t["created_at"])
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"tasks": tasks, "settings": settings},
    )


@app.get("/health")
async def health():
    err = await mdcx_healthcheck()
    return {
        "ok": err is None,
        "mdcx": "ok" if err is None else err,
    }


# ============================================================
# Submit
# ============================================================

@app.post("/submit")
async def submit(request: Request, text: str = Form(...)):
    text = text.strip()
    if not text:
        raise HTTPException(400, "empty input")

    kind, hints = classify(text)
    log.info("submit kind=%s hints=%s text=%s", kind, hints, text[:80])

    if kind == "jav_magnet" or kind == "jav_torrent":
        return await _handle_jav(text, kind, hints)
    if kind == "magnet" or kind == "torrent":
        return await _handle_regular_magnet(text, kind, hints)
    if kind == "media_name":
        return await _handle_media_name(text, hints)
    if kind == "jav_code":
        # Phase 1 will implement bare-code → 馒头 PT search → list candidates.
        # For now: refuse with a clear message.
        return JSONResponse(
            status_code=400,
            content={
                "error": "jav_code search not implemented yet (Phase 1 TODO)",
                "hint": "paste the magnet link directly for now",
            },
        )
    raise HTTPException(400, f"unknown kind: {kind}")


async def _handle_jav(text: str, kind: str, hints: dict) -> JSONResponse:
    qbt = QbtClient()
    try:
        await qbt.add_url(
            text,
            category=settings.qbt_jav_category,
            save_path=settings.qbt_jav_savepath,
        )
    finally:
        await qbt.close()

    tid = store.add(
        kind=kind,
        input_text=text,
        state="downloading",
        title=hints.get("name", "")[:200],
    )
    return JSONResponse({
        "task_id": tid,
        "kind": kind,
        "message": "added to qBT JAV category — watcher will run mdcx after download",
    })


async def _handle_regular_magnet(text: str, kind: str, hints: dict) -> JSONResponse:
    """Hand off to MoviePilot — it'll identify TMDB and route through normal flow."""
    mp = MpClient()
    name = hints.get("name") or "magnet-unknown"
    resp = await mp.add_download(title=name, enclosure=text)
    tid = store.add(
        kind=kind,
        input_text=text,
        state="submitted_to_mp" if resp.get("success") else "mp_rejected",
        title=name[:200],
        mp_response=resp,
    )
    return JSONResponse({
        "task_id": tid,
        "kind": kind,
        "mp_response": resp,
    })


async def _handle_media_name(text: str, hints: dict) -> JSONResponse:
    """Search MoviePilot for the title; return candidates for the user to pick."""
    mp = MpClient()
    candidates = await mp.search_media(text)
    tid = store.add(
        kind="media_name",
        input_text=text,
        state="search_done",
        mp_response={"candidates_count": len(candidates)},
    )
    return JSONResponse({
        "task_id": tid,
        "kind": "media_name",
        "candidates": candidates[:10],
    })


@app.post("/subscribe")
async def subscribe(
    name: str = Form(...),
    tmdbid: int = Form(...),
    type_: str = Form(..., alias="type"),
    season: int | None = Form(None),
):
    mp = MpClient()
    resp = await mp.subscribe(name=name, tmdbid=tmdbid, type_=type_, season=season)
    tid = store.add(
        kind="subscribe",
        input_text=f"{name} (tmdb:{tmdbid}, {type_})",
        state="subscribed" if resp.get("success") else "subscribe_failed",
        title=name[:200],
        mp_response=resp,
    )
    return JSONResponse({"task_id": tid, "mp_response": resp})


@app.get("/tasks")
async def tasks_api(limit: int = 50):
    return store.list_recent(limit=limit)


@app.get("/tasks/{task_id}")
async def task_detail(task_id: str):
    t = store.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t
