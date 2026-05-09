"""FastAPI entrypoint: web UI + submit endpoint + background watcher."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import metrics as m
from . import notify
from . import store
from .classifier import classify
from .config import settings
from . import cloud115, cloud115_watcher, cover_refill, discover, gfriends, img_proxy, jav_search, media_fallback, post_download, setup_wizard
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
    store.init_retry_state()
    cloud115.init_token_table()
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
    cloud115_task = asyncio.create_task(
        cloud115_watcher.cloud115_watch_loop(stop_event), name="cloud115_watcher",
    )

    try:
        yield
    finally:
        log.info("shutting down")
        stop_event.set()
        for t in (watcher_task, cloud115_task):
            try:
                await asyncio.wait_for(t, timeout=10)
            except asyncio.TimeoutError:
                t.cancel()


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
    from . import bangumi as bgm
    mdcx_err = await mdcx_healthcheck()
    tg_err = await notify.healthcheck()
    bgm_err = await bgm.healthcheck()
    c115_err = await cloud115.healthcheck()
    return {
        "ok": mdcx_err is None,
        "mdcx": "ok" if mdcx_err is None else mdcx_err,
        "telegram": "ok" if tg_err is None else tg_err,
        "bangumi": "ok" if bgm_err is None else bgm_err,
        "cloud115": "ok" if c115_err is None else c115_err,
    }


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint. Served from the default global registry."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================================
# First-run setup wizard — surfaces mdcx detection / install / config
# in the web UI so the user doesn't need to SSH in to edit .env.
# ============================================================

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Render the setup page. The page itself queries the JSON endpoints
    below for live status — keeps the template simple."""
    return templates.TemplateResponse(
        request=request, name="setup.html", context={},
    )


@app.get("/api/setup/status")
async def api_setup_status():
    """Combined status for the setup page: current mdcx detection +
    cached config snapshot for MoviePilot / qBT / Jellyfin / 115 (no
    live probe here — that happens on "Test connection" click via the
    dedicated endpoints below; keeps this endpoint cheap enough to
    poll on a timer from /index).

    115 special case: surfaces both the auth status (whether tokens
    are persisted) and the configured save_dir_id. Users without a
    115 membership see ``authorized=false`` and the wizard renders an
    "Authorize" CTA instead of credential fields. Truly opt-in.

    Also reads mdcx's own ``failed_output_folder`` via ``mdcx config get``
    so the wizard can surface the ``mp-relay failed_output_dir`` /
    ``mdcx failed_output_folder`` interaction (mdcx's value, if set,
    means mdcx moves first and mp-relay's holding is a no-op).

    Surfaces the wider set of mdcx fields (``success_output_folder``,
    ``website_single``, ``scrape_like``, plus a few read-only flags) so
    the /setup mdcx card can show "mdcx 关键配置" without the user opening
    the GUI for each."""
    mdcx = await setup_wizard.detect()
    mdcx_surfaced = await setup_wizard.mdcx_get_surfaced_config()
    mdcx["mdcx_failed_output_folder"] = mdcx_surfaced.get("failed_output_folder")
    mdcx["mdcx_surfaced"] = mdcx_surfaced
    mdcx["mdcx_editable_fields"]  = list(setup_wizard.MDCX_EDITABLE_FIELDS)
    mdcx["mdcx_readonly_expects"] = setup_wizard.MDCX_READONLY_FIELDS
    return {
        "mdcx": mdcx,
        "failed_output_dir": settings.failed_output_dir,
        "moviepilot": {
            "url": settings.mp_url,
            "user": settings.mp_user,
            "has_password": bool(settings.mp_pass),
        },
        "qbt": {
            "url": settings.qbt_url,
            "user": settings.qbt_user,
            "has_password": bool(settings.qbt_pass),
        },
        "jellyfin": {
            "url": settings.jellyfin_url,
            "has_api_key": bool(settings.jellyfin_api_key),
        },
        "cloud115": {
            "authorized": cloud115.is_authorized(),
            "save_dir_id": settings.cloud115_save_dir_id,
            "auth_url": "/auth/115",
        },
        "install": setup_wizard.install_status(since=0),
    }


# Empty form values mean "keep current" — UX promise from the placeholder
# text "(unchanged if blank)". These helpers fall back to settings.
def _or_current(value: str, current: str) -> str:
    return value if value else current


@app.post("/api/setup/moviepilot/test")
async def api_setup_moviepilot_test(
    url: str = Form(""), user: str = Form(""), password: str = Form(""),
):
    """Probe MoviePilot creds without saving. UI calls this on "Test
    connection" so the user can validate before they commit. Any blank
    field is filled in from the running ``settings`` so the user can
    re-test with just the password column without retyping everything."""
    return await setup_wizard.probe_moviepilot(
        _or_current(url, settings.mp_url),
        _or_current(user, settings.mp_user),
        _or_current(password, settings.mp_pass),
    )


@app.post("/api/setup/moviepilot/save")
async def api_setup_moviepilot_save(
    url: str = Form(""), user: str = Form(""), password: str = Form(""),
):
    """Test-then-persist. We always test before save so we never write a
    config known-broken. 422-on-fail prompts the UI to surface the error
    inline rather than silently saving bad data."""
    final_url  = _or_current(url, settings.mp_url)
    final_user = _or_current(user, settings.mp_user)
    final_pass = _or_current(password, settings.mp_pass)

    probe = await setup_wizard.probe_moviepilot(final_url, final_user, final_pass)
    if not probe["ok"]:
        raise HTTPException(422, probe["error"])
    updates = {"MP_URL": final_url, "MP_USER": final_user, "MP_PASS": final_pass}
    setup_wizard.write_env_keys(updates)
    setup_wizard.apply_settings_in_place(updates)
    return {"ok": True, "applied": {"MP_URL": final_url, "MP_USER": final_user, "MP_PASS": "***"}}


@app.post("/api/setup/qbt/test")
async def api_setup_qbt_test(
    url: str = Form(""), user: str = Form(""), password: str = Form(""),
):
    return await setup_wizard.probe_qbt(
        _or_current(url, settings.qbt_url),
        _or_current(user, settings.qbt_user),
        _or_current(password, settings.qbt_pass),
    )


@app.post("/api/setup/qbt/save")
async def api_setup_qbt_save(
    url: str = Form(""), user: str = Form(""), password: str = Form(""),
):
    final_url  = _or_current(url, settings.qbt_url)
    final_user = _or_current(user, settings.qbt_user)
    final_pass = _or_current(password, settings.qbt_pass)

    probe = await setup_wizard.probe_qbt(final_url, final_user, final_pass)
    if not probe["ok"]:
        raise HTTPException(422, probe["error"])
    updates = {"QBT_URL": final_url, "QBT_USER": final_user, "QBT_PASS": final_pass}
    setup_wizard.write_env_keys(updates)
    setup_wizard.apply_settings_in_place(updates)
    return {"ok": True, "applied": {"QBT_URL": final_url, "QBT_USER": final_user, "QBT_PASS": "***"}}


@app.post("/api/setup/jellyfin/test")
async def api_setup_jellyfin_test(url: str = Form(""), api_key: str = Form("")):
    return await setup_wizard.probe_jellyfin(
        _or_current(url, settings.jellyfin_url),
        _or_current(api_key, settings.jellyfin_api_key),
    )


@app.post("/api/setup/jellyfin/save")
async def api_setup_jellyfin_save(url: str = Form(""), api_key: str = Form("")):
    final_url = _or_current(url, settings.jellyfin_url)
    final_key = _or_current(api_key, settings.jellyfin_api_key)
    probe = await setup_wizard.probe_jellyfin(final_url, final_key)
    if not probe["ok"]:
        raise HTTPException(422, probe["error"])
    updates = {"JELLYFIN_URL": final_url, "JELLYFIN_API_KEY": final_key}
    setup_wizard.write_env_keys(updates)
    setup_wizard.apply_settings_in_place(updates)
    return {"ok": True, "applied": {"JELLYFIN_URL": final_url, "JELLYFIN_API_KEY": "***"}}


@app.post("/api/setup/configure")
async def api_setup_configure(mdcx_dir: str = Form(...)):
    """Path B: user has mdcx already installed; they tell us where.

    We probe the dir for a working CLI module, write the resolved
    {dir, python, module} into .env, AND mutate the running settings
    in-place so the next mdcx call uses them — no service restart
    required.
    """
    result = await setup_wizard.validate_path(mdcx_dir)
    if not result["ok"]:
        raise HTTPException(400, result["error"])

    updates = {
        "MDCX_DIR": mdcx_dir,
        "MDCX_PYTHON": result["python"],
        "MDCX_MODULE": result["module"],
    }
    setup_wizard.write_env_keys(updates)
    setup_wizard.apply_settings_in_place(updates)

    # Re-probe so the response confirms the new config actually works.
    return {"ok": True, "applied": updates, "health": await setup_wizard.detect()}


@app.post("/api/setup/failed-output-dir")
async def api_setup_failed_output_dir(failed_output_dir: str = Form("")):
    """Save the ``failed_output_dir`` setting (where mp-relay moves
    staging dirs after a scrape/QC failure). Empty value = use the
    sibling-collector default (<staging-parent>/scrapefailed/<basename>/).

    No path validation here — mp-relay creates the dir lazily on first
    failure. We don't want to fail save just because the dir doesn't
    exist yet (a non-existent path may be on a removable drive)."""
    final = (failed_output_dir or "").strip()
    setup_wizard.write_env_keys({"FAILED_OUTPUT_DIR": final})
    setup_wizard.apply_settings_in_place({"FAILED_OUTPUT_DIR": final})
    return {"ok": True, "applied": {"FAILED_OUTPUT_DIR": final or "(empty — sibling-collector default)"}}


@app.post("/api/setup/mdcx-config-set")
async def api_setup_mdcx_config_set(key: str = Form(...), value: str = Form("")):
    """Mutate a single mdcx config field via mdcx's own CLI. Restricted
    to the whitelist mp-relay surfaces (``MDCX_EDITABLE_FIELDS``) — any
    other key is rejected before we even invoke mdcx. mdcx itself also
    has its own ``_FIELDS`` whitelist as a second-level guard, so even
    if our list drifted, mdcx would refuse unsafe writes.

    Empty string is a valid value for path-type fields (means "unset")."""
    if key not in setup_wizard.MDCX_EDITABLE_FIELDS:
        raise HTTPException(
            400,
            f"key {key!r} not in surface allow-list. Allowed: {setup_wizard.MDCX_EDITABLE_FIELDS}",
        )
    result = await setup_wizard.mdcx_config_set(key, value)
    if not result.get("ok"):
        # mdcx printed the validation failure to stderr — surface it
        raise HTTPException(422, f"mdcx rejected {key}={value!r}: {result.get('stderr', 'unknown')}")
    # Return the new effective value as a confirmation
    new_val = await setup_wizard.mdcx_config_get(key)
    return {"ok": True, "key": key, "value": new_val}


@app.post("/api/setup/mdcx-takeover-failed")
async def api_setup_mdcx_takeover_failed():
    """Tell mdcx to STOP moving files on failure (sets mdcx config's
    ``failed_output_folder`` to empty string via ``mdcx config set``).

    With mdcx's failed_output_folder empty, mdcx leaves files in their
    staging location after a failed scrape. mp-relay's
    ``_move_to_failed_holding`` then catches them and moves to its
    configured location. Without this, mdcx moves first and mp-relay's
    holding stays empty.

    Idempotent: setting an already-empty field is a no-op. Whitelist-
    enforced by mdcx itself (failed_output_folder is in mdcx's _FIELDS
    so the set is allowed)."""
    result = await setup_wizard.mdcx_config_set("failed_output_folder", "")
    if not result.get("ok"):
        raise HTTPException(502, f"mdcx config set failed: {result.get('stderr', 'unknown')}")
    return {
        "ok": True,
        "message": "mdcx 不再移动失败的 staging — mp-relay 的 failed_output_dir 接管所有失败 holding",
        "stdout": result.get("stdout", ""),
    }


@app.post("/api/setup/install")
async def api_setup_install(script: str = Form("setup-mdcx")):
    """Path A: trigger one of the bundled setup PS1 scripts (currently
    ``setup-mdcx`` or ``setup-moviepilot``) in the background. Returns
    immediately; poll /api/setup/install/log for progress.

    Only one install runs at a time — the script-name parameter just
    picks which one to launch when the slot is free."""
    result = await setup_wizard.start_install(script=script)
    if not result["ok"]:
        raise HTTPException(409, result["error"])
    return result


@app.get("/api/setup/install/log")
async def api_setup_install_log(since: int = 0):
    """Tail the install log. ``since`` is the cursor returned by the
    previous call's ``next_since``."""
    return setup_wizard.install_status(since=since)


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
        m.SUBMIT_TOTAL.labels(kind=kind, result="duplicate").inc()
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
    try:
        handler_resp = await _dispatch(text, kind, hints)
    except Exception:
        m.SUBMIT_TOTAL.labels(kind=kind, result="error").inc()
        raise
    m.SUBMIT_TOTAL.labels(kind=kind, result="accepted").inc()

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
    """Search MoviePilot for the title; return candidates for the user to pick.

    Phase 1.5 / 1.6 fallback chain: when MP returns zero candidates, query
    AniList + Bangumi concurrently for alternate titles (English / romaji /
    native / synonyms / Chinese-fan-translation), re-search MP with each, and
    surface the Bangumi match in the response even when MP still comes up
    empty (so the user at least sees what the work actually is + a bgm.tv
    link to take it from there).
    """
    mp = MpClient()
    candidates = await mp.search_media(text)

    fallback_used: list[dict] = []
    bangumi_match: Optional[dict] = None
    if not candidates:
        # Run both alt-title sources concurrently + a Bangumi single-match
        # probe in parallel. Bangumi match is useful even if MP retry fails.
        alts_task = asyncio.create_task(media_fallback.alternate_titles_all(text, limit=8))
        bangumi_task = asyncio.create_task(media_fallback.find_bangumi_match(text))
        alts = await alts_task
        bangumi_match = await bangumi_task

        for alt in alts:
            try:
                alt_candidates = await mp.search_media(alt["title"])
            except Exception as e:
                log.warning("media_fallback re-search failed for %s: %s", alt["title"], e)
                continue
            if alt_candidates:
                # Tag each candidate with how we found it so UI can show the path.
                for c in alt_candidates:
                    c.setdefault("_via", alt["via"])
                    c.setdefault("_alt_title", alt["title"])
                candidates.extend(alt_candidates)
                fallback_used.append({"title": alt["title"], "via": alt["via"], "found": len(alt_candidates)})
            if len(candidates) >= 10:
                break

    tid = store.add(
        kind="media_name",
        input_text=text,
        state="search_done" if candidates else "search_empty",
        mp_response={
            "candidates_count": len(candidates),
            "fallback_used": fallback_used,
            "bangumi_match": bangumi_match,
        },
    )
    payload: dict = {
        "task_id": tid,
        "kind": "media_name",
        "candidates": candidates[:10],
    }
    if fallback_used:
        payload["fallback_used"] = fallback_used
    if bangumi_match:
        payload["bangumi_match"] = bangumi_match
    if not candidates:
        payload["hint"] = (
            "MoviePilot / AniList / Bangumi 都没在 TMDB 找到。可以试试: "
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


@app.get("/api/jav-keyword-search")
async def api_jav_keyword_search(q: str, limit: int = 20):
    """Free-text magnet search via sukebei — no code-strict filter.

    Use case: user got a Bangumi match for a Chinese fan-translation, the JP
    name is now known, but it's not a JAV-style 番号. This lets the UI feed
    the JP name directly to sukebei for a torrent search without forcing it
    through the code-search ranker.
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "q required")
    candidates = await jav_search.search_keyword(q, limit=limit)
    return {"keyword": q, "candidates": candidates, "total": len(candidates)}


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


@app.post("/api/bulk-115")
async def api_bulk_115(codes_csv: str = Form(...)):
    """Phase 2 batch path — 115 cloud-offline variant of ``/api/bulk-subscribe``.

    Same auto-pick logic (best_candidate by suspicion/中字/seeders/quality/size),
    but instead of feeding qBT we push each magnet to 115's offline queue. The
    ``cloud115_watcher`` background loop will then pull completed downloads back
    to the local staging dir and dispatch the post-download pipeline — so the
    only difference vs. qBT from the user's POV is "where the bytes come from".

    Returns the same per-code report shape so the UI can mark each tile.
    Returns 409 with ``auth_url`` if 115 isn't authorized — so the UI can
    redirect the user to /auth/115 instead of failing every code in the batch.
    """
    if not cloud115.is_authorized():
        return JSONResponse(
            status_code=409,
            content={
                "error": "unauthorized",
                "auth_url": "/auth/115",
                "hint": "115 还未授权 — 先去 /auth/115 扫码",
            },
        )

    codes = [c.strip().upper() for c in codes_csv.split(",") if c.strip()]
    if not codes:
        raise HTTPException(400, "codes_csv empty")

    results: list[dict] = []
    for code in codes:
        try:
            candidates = await jav_search.search_jav_code(code, limit=20)
            best = jav_search.best_candidate(candidates)
            if not best:
                results.append({"code": code, "ok": False, "reason": "no candidates"})
                continue

            resp = await cloud115.add_offline_url(best["magnet"])
            # Mirror the single-add error handling in /api/cloud115-add: 115 may
            # return state=False with a quota / dup-task message instead of raising.
            if isinstance(resp, dict) and resp.get("state") is False:
                msg = resp.get("message") or resp.get("error_msg") or str(resp)
                store.add(
                    kind="cloud_offline_115",
                    input_text=f"bulk: {code}",
                    state="cloud_failed",
                    title=best["title"][:200],
                    error=msg[:300],
                )
                results.append({"code": code, "ok": False, "reason": msg[:200]})
                continue

            data = resp.get("data") or {}
            first = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            info_hash = first.get("info_hash") or best.get("info_hash") or ""
            name = first.get("name") or best["title"]

            store.add(
                kind="cloud_offline_115",
                input_text=f"bulk: {code}",
                state="submitted_to_115",
                hash=info_hash,
                title=(name or code)[:200],
            )
            results.append({
                "code": code, "ok": True,
                "picked": {
                    "title": best["title"],
                    "size": best["size_str"],
                    "seeders": best["seeders"],
                    "quality_score": best["quality_score"],
                    "info_hash": info_hash,
                    "name": name,
                },
            })
        except Exception as e:
            log.exception("bulk 115 %s failed: %s", code, e)
            results.append({"code": code, "ok": False, "reason": str(e)[:200]})

    ok_count = sum(1 for r in results if r["ok"])
    return {"total": len(codes), "ok": ok_count, "failed": len(codes) - ok_count, "results": results}


@app.get("/api/discover/films")
async def api_discover_films(kind: str = "", id: str = "", url: str = "",
                              refresh: bool = False):
    """Phase 2c — list films for a series / studio / genre / director / actor.

    Caller supplies either:
      - kind + id  (e.g. kind=series, id=RPC), OR
      - url        (a JavBus URL we'll parse — paste-friendly)
    """
    # Allow URL paste shortcut
    if url and not (kind and id):
        parsed = discover.parse_javbus_url(url)
        if parsed is None:
            return JSONResponse({"error": "could not parse JavBus URL"}, status_code=400)
        kind, id = parsed

    if not kind or not id:
        return JSONResponse({"error": "kind+id (or url) required"}, status_code=400)

    try:
        films = await discover.films_by_kind(kind, id, force_refresh=refresh)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    discover.annotate_owned(films)
    owned_count = sum(1 for f in films if f.get("owned"))
    return JSONResponse({
        "kind": kind,
        "id": id,
        "films": films,
        "total": len(films),
        "owned_count": owned_count,
    })


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


# ============================================================
# Phase 2d: gfriends actor avatar fallback
# ============================================================

@app.get("/api/gfriends")
async def api_gfriends(name: str = ""):
    """Look up an actor's portrait URL on gfriends/gfriends.

    Returns ``{name, url}`` on hit, ``{name, url: null}`` on miss. Used by
    the discover UI as a fallback when JavBus actor cards have no photo.
    """
    if not name:
        raise HTTPException(400, "name required")
    url = await gfriends.find_actor_avatar_url(name)
    return {"name": name, "url": url}


# ============================================================
# Hotlinked-image proxy (for JavBus / Bangumi covers etc.)
# ============================================================

@app.get("/api/img-proxy")
async def api_img_proxy(url: str):
    """Fetch an image from a whitelisted hotlink-protected host with the
    correct Referer, stream it back to the browser. Workaround for JavBus
    Cloudflare 403-without-Referer + browser inability to set Referer.

    Hosts: see ``img_proxy._ALLOWED_HOSTS`` (javbus / bgm.tv / dmm).
    Caching: in-process LRU; client also gets ``Cache-Control: max-age=86400``
    so the browser caches across page loads.
    """
    if not url:
        raise HTTPException(400, "url required")
    if not img_proxy.is_allowed(url):
        raise HTTPException(403, "host not on proxy allowlist")
    result = await img_proxy.fetch(url)
    if result is None:
        raise HTTPException(404, "image unavailable")
    body, content_type = result
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/cover-refill")
async def api_cover_refill(
    root: str = Form(...),
    dry_run: bool = Form(True),
    limit: Optional[int] = Form(None),
):
    """Refill missing cover images in a Jellyfin library by reading each
    folder's NFO and pulling the cover from JavDB's CDN.

    Body params (form):
      - ``root``     — library root, e.g. ``E:/Jav``. Required.
      - ``dry_run``  — default True; reports what *would* be written.
      - ``limit``    — cap candidates (useful for first-time spot-check).

    Returns summary + per-folder result. See ``cover_refill.refill_root``.
    """
    if not root or not Path(root).is_dir():
        raise HTTPException(400, f"root must be an existing directory: {root!r}")
    return await cover_refill.refill_root(root, dry_run=dry_run, limit=limit)


# ============================================================
# Phase 1.8: 115 cloud-drive offline download (OAuth)
# ============================================================

@app.get("/auth/115", response_class=HTMLResponse)
async def auth_115_page(request: Request):
    """One-time QR-scan authorization page. After this, mp-relay can push
    magnets to 115's offline-download queue without ever asking again
    (refresh tokens auto-rotate).
    """
    authorized = cloud115.is_authorized()
    return templates.TemplateResponse(
        request=request,
        name="auth_115.html",
        context={"authorized": authorized},
    )


@app.post("/api/cloud115/start")
async def api_cloud115_start():
    """Begin device-code flow. Returns the QR-scan handle for the auth page."""
    try:
        return await cloud115.start_auth()
    except Exception as e:
        log.exception("cloud115 start_auth failed: %s", e)
        raise HTTPException(500, f"start_auth failed: {e}")


@app.get("/api/cloud115/poll")
async def api_cloud115_poll(uid: str, time: str, sign: str):
    """Poll the QR scan status. When status flips to 2, server-side
    auto-exchanges the device code for tokens and returns authorized=true."""
    try:
        return await cloud115.poll_auth(uid, time, sign)
    except Exception as e:
        log.warning("cloud115 poll_auth failed: %s", e)
        return {"status": -1, "msg": str(e), "authorized": False}


@app.post("/api/cloud115/clear")
async def api_cloud115_clear():
    """Forget current authorization (use this if tokens go stale and need
    fresh QR scan). Idempotent."""
    cloud115.clear_tokens()
    return {"cleared": True}


@app.post("/api/cloud115-add")
async def api_cloud115_add(magnet: str = Form(...), code: str = Form("")):
    """Push a magnet to 115's offline queue. Records a task so it shows up in
    the live tasks table.
    """
    if not magnet.startswith("magnet:") and not magnet.startswith("http"):
        raise HTTPException(400, "magnet (or http:// / ed2k:) URL required")
    if not cloud115.is_authorized():
        return JSONResponse(
            status_code=409,
            content={
                "error": "unauthorized",
                "auth_url": "/auth/115",
                "hint": "115 还未授权 — 先去 /auth/115 扫码",
            },
        )

    try:
        resp = await cloud115.add_offline_url(magnet)
    except Exception as e:
        log.exception("cloud115 add failed: %s", e)
        tid = store.add(
            kind="cloud_offline_115",
            input_text=magnet[:200],
            state="cloud_failed",
            title=code or "(unknown)",
            error=str(e)[:300],
        )
        raise HTTPException(502, f"115 add_offline_url failed: {e}")

    # Successful response shape per 115 docs:
    # {"state": True, "code": 0, "message": "", "data": [{"info_hash": ..., "name": ..., "size": ...}]}
    state = resp.get("state", True)
    if not state:
        msg = resp.get("message") or resp.get("error_msg") or str(resp)
        tid = store.add(
            kind="cloud_offline_115",
            input_text=magnet[:200],
            state="cloud_failed",
            title=code or "(unknown)",
            error=msg[:300],
        )
        return JSONResponse(status_code=502, content={
            "task_id": tid, "error": msg, "raw": resp,
        })

    data = resp.get("data") or {}
    # 115's response sometimes returns a single dict, sometimes a list per task.
    first = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    info_hash = first.get("info_hash") or ""
    name = first.get("name") or ""

    tid = store.add(
        kind="cloud_offline_115",
        input_text=magnet[:200],
        state="submitted_to_115",
        hash=info_hash,
        title=name or code or "(unknown)",
    )
    return {
        "task_id": tid,
        "info_hash": info_hash,
        "name": name,
        "state": "submitted_to_115",
    }


@app.get("/api/cloud115/list")
async def api_cloud115_list(page: int = 1):
    """Pass-through to 115's offline list — useful for a 'check progress on
    115' button if/when we add one."""
    if not cloud115.is_authorized():
        raise HTTPException(409, "115 unauthorized; visit /auth/115")
    return await cloud115.list_offline(page=page)


@app.post("/api/cloud115/retry-failed-scrapes")
async def api_cloud115_retry_failed_scrapes():
    """Re-run the mdcx step for tasks stuck in ``scrape_no_match`` or
    ``scrape_failed_items``.

    The file is already on local disk (``save_path`` was recorded when sync
    succeeded), so we just dispatch ``_scrape_and_postclean`` again. Useful
    after fixing whatever was making mdcx return total=0 (e.g. the fork's
    ``manager.load()`` clobbering ``media_path`` — fixed in mp-relay 2026-05-05
    by switching ``mdcx_runner.scrape_dir`` to per-file ``scrape file``).

    Runs the retries in the background — endpoint returns immediately with
    a count. State transitions show up in the live tasks table as each
    completes.
    """
    candidate_states = ["scrape_no_match", "scrape_failed_items", "scrape_failed"]
    rows = store.list_in_states(candidate_states, kind="cloud_offline_115", limit=500)
    n = 0
    skipped = 0
    for r in rows:
        save_path = r.get("save_path")
        if not save_path:
            skipped += 1
            continue
        tid = r["id"]
        name = r.get("title") or "(unknown)"
        # Bump to processing so the UI reflects "in flight"; watcher won't
        # touch it (only scans submitted_to_115).
        store.update(tid, state="processing", error=None)
        # Fire-and-forget — _scrape_and_postclean writes its own terminal state.
        asyncio.create_task(
            post_download._scrape_and_postclean(save_path, tid, name),
            name=f"retry-scrape-{tid[:8]}",
        )
        n += 1
    return {"requeued": n, "skipped_no_save_path": skipped}


@app.post("/api/cloud115/retry-failed-syncs")
async def api_cloud115_retry_failed_syncs():
    """Re-queue tasks stuck in ``cloud_sync_failed`` so the watcher picks
    them up on its next tick.

    Background: if 115's access token expires mid-batch, every queued sync
    blows up with ``download_url_info_open failed: access_token 无效`` and
    transitions to ``cloud_sync_failed``. The watcher's pending-state
    filter is ``submitted_to_115`` only — by design, so a permanently bad
    task doesn't loop forever — so those failed syncs sit there until
    something flips them back. This endpoint is that "something". Use it
    after fixing whatever caused the original failure (token roll, network
    blip, etc.).

    Returns the count re-queued. Watcher will pick them up within
    ``cloud115_poll_interval_sec`` seconds (default 60).
    """
    rows = store.list_in_states(["cloud_sync_failed"], kind="cloud_offline_115", limit=500)
    n = 0
    for r in rows:
        store.update(r["id"], state="submitted_to_115", error=None)
        n += 1
    return {"requeued": n}
