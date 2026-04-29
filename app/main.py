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
from . import discover, jav_search
from .exists import check_input as check_existence, extract_code as extract_jav_code
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
    from .config import validate as validate_settings
    issues = validate_settings()
    if issues:
        for msg in issues:
            log.error("CONFIG: %s", msg)
        log.error("Fix .env and restart. Service will start but most operations will fail.")

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

@app.post("/check")
async def check(text: str = Form(...)):
    """Look up whether the input already exists locally — without submitting anything."""
    text = text.strip()
    if not text:
        raise HTTPException(400, "empty input")

    kind, hints = classify(text)
    existence = await check_existence(text, kind, hints)
    return JSONResponse({
        "kind": kind,
        "hints": hints,
        **existence,
    })


@app.post("/submit")
async def submit(
    request: Request,
    text: str = Form(...),
    force: bool = Form(False),
):
    """Submit input. If existence detected and force=False, return 409 with details.

    UI is expected to call /submit; if it gets 409, show the user a confirmation
    UI and re-POST with force=true.
    """
    text = text.strip()
    if not text:
        raise HTTPException(400, "empty input")

    kind, hints = classify(text)
    log.info("submit kind=%s hints=%s force=%s text=%s",
             kind, hints, force, text[:80])

    # Existence pre-check — always run, used for warning (and for blocking magnets).
    existence = await check_existence(text, kind, hints)
    existing_jav = existence.get("existing_jav") or []
    existing_media = existence.get("existing_media") or {}
    has_dup = bool(
        existing_jav
        or existing_media.get("subscriptions")
        or existing_media.get("downloads")
    )

    # Magnets / torrents: block on duplicate so user must explicitly force.
    # media_name returns search candidates — informational warning only.
    if has_dup and not force and kind in (
        "jav_magnet", "jav_torrent", "magnet", "torrent"
    ):
        return JSONResponse(
            status_code=409,
            content={
                "duplicate": True,
                "kind": kind,
                "hints": hints,
                **existence,
                "hint": "resubmit with force=true to add anyway",
            },
        )

    # Normal dispatch — handler returns its own JSONResponse / dict.
    handler_resp = await _dispatch(text, kind, hints)

    # Annotate response with existence info so UI can show warning even on 200.
    return _attach_existence(handler_resp, existence)


async def _dispatch(text: str, kind: str, hints: dict):
    if kind in ("jav_magnet", "jav_torrent"):
        return await _handle_jav(text, kind, hints)
    if kind in ("magnet", "torrent"):
        return await _handle_regular_magnet(text, kind, hints)
    if kind == "id_ref":
        return await _handle_id_ref(text, hints)
    if kind == "media_name":
        return await _handle_media_name(text, hints)
    if kind == "jav_code":
        return await _handle_jav_code(text, hints)
    raise HTTPException(400, f"unknown kind: {kind}")


async def _handle_jav_code(text: str, hints: dict) -> JSONResponse:
    """User pasted a bare 番号 — search sukebei and return ranked magnet candidates."""
    code = (hints.get("code") or text).strip().upper()
    candidates = await jav_search.search_jav_code(code, limit=20)
    tid = store.add(
        kind="jav_code",
        input_text=text,
        state="search_done" if candidates else "search_empty",
        title=code,
    )
    if not candidates:
        return JSONResponse({
            "task_id": tid,
            "kind": "jav_code",
            "code": code,
            "candidates": [],
            "hint": "sukebei 没找到这个番号的种。可以试试: 1) 直接贴磁力链; 2) 等几小时再搜（新发的种需要时间被索引）",
        })
    return JSONResponse({
        "task_id": tid,
        "kind": "jav_code",
        "code": code,
        "candidates": candidates,
        "message": f"找到 {len(candidates)} 个种子候选",
    })


def _attach_existence(resp: JSONResponse, existence: dict) -> JSONResponse:
    """Merge existence info into a JSONResponse body."""
    import json
    try:
        body = json.loads(resp.body.decode("utf-8"))
    except Exception:
        return resp
    if isinstance(body, dict):
        body.setdefault("existing_jav", existence.get("existing_jav") or [])
        body.setdefault("existing_media", existence.get("existing_media") or {})
        if existence.get("jav_code"):
            body.setdefault("jav_code", existence["jav_code"])
        return JSONResponse(content=body, status_code=resp.status_code)
    return resp


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
    payload: dict = {
        "task_id": tid,
        "kind": "media_name",
        "candidates": candidates[:10],
    }
    if not candidates:
        payload["hint"] = (
            "MoviePilot 没找到该标题。可以试试: "
            "1) 英文/日文原名 (e.g. 'Nope' instead of '不'); "
            "2) 直接贴 TMDB ID (e.g. 'tmdb:762504'); "
            "3) 贴 TMDB 详情页 URL (e.g. 'https://www.themoviedb.org/movie/762504'); "
            "4) 贴磁力链或 .torrent URL"
        )
    return JSONResponse(payload)


async def _handle_id_ref(text: str, hints: dict) -> JSONResponse:
    """User pasted tmdb:NNN / imdb tt-id / TMDB or Douban URL. Skip search."""
    mp = MpClient()
    detail = await mp.media_detail(
        id_type=hints.get("id_type"),
        id_value=hints.get("id_value"),
        media_type=hints.get("media_type"),
    )
    if not detail:
        tid = store.add(kind="id_ref", input_text=text, state="not_found",
                        mp_response={"error": "media not found in MP/TMDB"})
        return JSONResponse({
            "task_id": tid,
            "kind": "id_ref",
            "candidates": [],
            "hint": f"MoviePilot 找不到 {hints.get('id_type')}={hints.get('id_value')} 对应的媒体；可能 ID 错误或 TMDB 暂不可达",
        })
    tid = store.add(kind="id_ref", input_text=text, state="resolved",
                    title=(detail.get("title") or detail.get("name") or "")[:200],
                    mp_response={"resolved_to": detail.get("title")})
    return JSONResponse({
        "task_id": tid,
        "kind": "id_ref",
        "candidates": [detail],   # single high-confidence candidate, UI shows subscribe button
        "message": f"已根据 {hints.get('id_type')}={hints.get('id_value')} 直接定位到媒体",
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


# ============================================================
# Phase 2: actor discovery
# ============================================================

@app.get("/discover", response_class=HTMLResponse)
async def discover_page(request: Request, name: str = "", actor_id: str = ""):
    """演员发现页面.

    /discover                     → 仅显示搜索框
    /discover?name=葵つかさ        → 搜索演员
    /discover?actor_id=xxx        → 直接展示该演员的作品列表
    """
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context={
            "settings": settings,
            "initial_name": name,
            "initial_actor_id": actor_id,
        },
    )


@app.get("/api/jav-search")
async def api_jav_search(code: str, refresh: bool = False, limit: int = 20):
    """Phase 1 — list magnet candidates for a 番号 (no submission)."""
    if not code:
        raise HTTPException(400, "code required")
    candidates = await jav_search.search_jav_code(code.upper(), limit=limit, force_refresh=refresh)
    return {"code": code.upper(), "candidates": candidates, "total": len(candidates)}


@app.post("/api/jav-add")
async def api_jav_add(magnet: str = Form(...), code: str = Form("")):
    """Add a single magnet to qBT JAV category. Used by /submit jav_code flow + Phase 2 batch."""
    if not magnet.startswith("magnet:"):
        raise HTTPException(400, "magnet must start with 'magnet:'")

    qbt = QbtClient()
    try:
        await qbt.add_url(
            magnet,
            category=settings.qbt_jav_category,
            save_path=settings.qbt_jav_savepath,
        )
    finally:
        await qbt.close()

    tid = store.add(
        kind="jav_magnet",
        input_text=magnet[:200],
        state="downloading",
        title=code or "(unknown)",
    )
    return {"task_id": tid, "code": code, "state": "downloading"}


@app.post("/api/bulk-subscribe")
async def api_bulk_subscribe(codes_csv: str = Form(...)):
    """Phase 2 batch path: take a comma-separated list of codes, search each on
    sukebei (using cache), pick the best candidate, add to qBT.

    Returns a per-code report so the UI can show what succeeded/failed.
    """
    codes = [c.strip().upper() for c in codes_csv.split(",") if c.strip()]
    if not codes:
        raise HTTPException(400, "codes_csv empty")

    # One concurrent qBT client for the whole batch
    qbt = QbtClient()
    results: list[dict] = []
    try:
        for code in codes:
            try:
                candidates = await jav_search.search_jav_code(code, limit=20)
                best = jav_search.best_candidate(candidates)
                if not best:
                    results.append({"code": code, "ok": False, "reason": "no candidates"})
                    continue
                await qbt.add_url(
                    best["magnet"],
                    category=settings.qbt_jav_category,
                    save_path=settings.qbt_jav_savepath,
                )
                store.add(
                    kind="jav_magnet",
                    input_text=f"bulk: {code}",
                    state="downloading",
                    title=best["title"][:200],
                )
                results.append({
                    "code": code, "ok": True,
                    "picked": {
                        "title": best["title"],
                        "size": best["size_str"],
                        "seeders": best["seeders"],
                        "quality_score": best["quality_score"],
                        "info_hash": best["info_hash"],
                    },
                })
            except Exception as e:
                log.exception("bulk subscribe %s failed: %s", code, e)
                results.append({"code": code, "ok": False, "reason": str(e)[:200]})
    finally:
        await qbt.close()

    ok_count = sum(1 for r in results if r["ok"])
    return {"total": len(codes), "ok": ok_count, "failed": len(codes) - ok_count, "results": results}


@app.get("/api/discover/actor")
async def api_discover_actor(name: str = "", actor_id: str = "",
                              refresh: bool = False):
    """JSON API for the discover page.

    Either provide `name` to search, or `actor_id` to fetch films.
    """
    if not name and not actor_id:
        return JSONResponse({"error": "either name or actor_id required"}, status_code=400)

    # If only name given: search for actor
    if name and not actor_id:
        actors = await discover.search_actor(name, force_refresh=refresh)
        return JSONResponse({"query": name, "actors": actors})

    # actor_id given: fetch films + annotate owned
    films = await discover.actor_films(actor_id, force_refresh=refresh)
    discover.annotate_owned(films)
    owned_count = sum(1 for f in films if f.get("owned"))
    return JSONResponse({
        "actor_id": actor_id,
        "films": films,
        "total": len(films),
        "owned_count": owned_count,
    })


@app.get("/tasks/{task_id}")
async def task_detail(task_id: str):
    t = store.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t
