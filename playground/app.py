"""The playground web service (Starlette, served by uvicorn on Cloud Run).

    GET  /                 the playground (sign-in page when signed out)
    GET  /p/<token>        sign in with a pass link
    POST /auth/google      sign in with a Google ID token
    POST /auth/signout
    GET  /viewer           the sandboxed X_ITE viewer the page embeds
    GET  /api/me           who you are, your limits, the writers, the versions
    POST /api/run          the loop, streamed as server-sent events
    POST /api/validate     validator report for a pasted scene (no model)
    POST /api/mutate       inject one known defect into a scene (no model)
    POST /api/runs/<id>/visible   the browser's verdict for a run's scene
    GET  /api/runs/<id>    one of your runs
    GET  /api/history      your recent runs
    GET  /api/arena        the judge bench numbers behind the arena panel
    GET  /admin            the owner's page; /api/admin/* behind it
    GET  /health
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (FileResponse, JSONResponse, RedirectResponse, Response,
                                 StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from playground import auth, mutate
from playground.config import STATIC, WRITERS, Settings, judge_for, public_writers
from playground.limits import Denied, Guard, effective_limits
from playground.llm import LLM
from playground.loop import RunRequest, run_loop
from playground.report import full_report
from playground.runlog import RunLog, row_for, visible_row
from playground.store import make_store

log = logging.getLogger("playground")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

PAGE_CSP = ("default-src 'self'; script-src 'self' https://accounts.google.com/gsi/client; "
            "frame-src 'self' https://accounts.google.com/gsi/; "
            "connect-src 'self' https://accounts.google.com/gsi/; "
            "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; "
            "img-src 'self' data: blob:; font-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# The viewer runs untrusted scenes (X3D Script nodes are JavaScript) inside an
# iframe sandboxed without allow-same-origin: an opaque origin with no cookies
# and no way to call the API, and no network beyond this server's assets.
VIEWER_CSP = ("default-src 'none'; script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval' blob:; "
              "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
              "connect-src 'self' data: blob:; worker-src 'self' blob:; media-src 'self' data: blob:; "
              "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = {"Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}

settings = Settings()


class State:
    store = None
    llm: LLM | None = None
    signer: auth.Signer | None = None
    guard: Guard | None = None
    runlog: RunLog | None = None
    arena: dict = {}
    started = time.time()


S = State()


@asynccontextmanager
async def lifespan(app):
    S.llm = LLM(settings)
    S.runlog = RunLog(settings, S.llm.tokens)
    S.store = make_store(settings, S.runlog)
    if hasattr(S.store, "load"):
        t0 = time.time()
        loaded = await S.store.load()
        log.info(json.dumps({"event": "journal-loaded", "s": round(time.time() - t0, 2), **loaded}))
    key = settings.session_key
    if not key:
        key = await S.store.put_kv_if_absent("session-key", os.urandom(32).hex())
    S.signer = auth.Signer(key)
    S.guard = Guard(S.store, settings.phase_label)
    arena_path = STATIC / "arena.json"
    S.arena = json.loads(arena_path.read_text()) if arena_path.exists() else {}
    if settings.bootstrap_admin_pass:
        if not await S.store.get_pass(settings.bootstrap_admin_pass):
            await S.store.put_pass(settings.bootstrap_admin_pass, {
                "label": "Owner", "email": "", "created": time.time(), "created_by": "bootstrap",
                "expires": 0, "revoked": False, "admin": True, "per_hour": None, "per_day": None,
                "uses": 0})
    # Before serving: on Cloud Run the CPU is throttled between requests, so a
    # background warm-up would stall and the first person would pay for it.
    await asyncio.to_thread(_warm)
    yield
    await S.llm.close()


def _warm():
    """Load both XSDs and the X3DUOM once, so the first person waits seconds less."""
    t0 = time.time()
    for v in ("4.0", "4.1"):
        full_report(f'<X3D profile="Immersive" version="{v}"><Scene><Viewpoint/></Scene></X3D>')
    log.info("validator warm in %.1fs", time.time() - t0)


# ---------------------------------------------------------------- helpers

def _ip(request: Request) -> str:
    # Cloud Run's front end appends the address it saw to X-Forwarded-For, so
    # the last entry is the one a client cannot forge; earlier ones can be.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "")


def _principal(request: Request) -> auth.Principal | None:
    return auth.principal_from_session(S.signer.read_cookie(request.cookies.get(auth.COOKIE, "")))


def _json(data, status=200, headers=None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={**SECURITY_HEADERS, **(headers or {})})


def _denied(d: Denied) -> JSONResponse:
    headers = {"Retry-After": str(d.retry_after)} if d.retry_after else {}
    return _json({"error": d.message, "reason": d.reason, "retry_after": d.retry_after}, 429, headers)


async def _event(kind: str, request: Request, p: auth.Principal | None, **detail):
    try:
        await S.store.add_event(kind, {
            "who": p.display if p else detail.pop("who", ""), "principal": p.id if p else "",
            "ip": auth.ip_key(_ip(request), S.signer),
            "ua": request.headers.get("user-agent", "")[:160], **detail})
    except Exception as exc:
        log.warning("event log failed: %s", exc)
    log.info(json.dumps({"event": kind, "principal": p.id if p else "", **{
        k: v for k, v in detail.items() if isinstance(v, (str, int, float, bool))}}))


async def _still_allowed(p: auth.Principal) -> bool:
    """The list is checked on every call, not only at sign-in: a revoked pass,
    a blocked address or a removed domain stops working within seconds, not
    when the 12-hour cookie runs out. Admin rights are recomputed the same way."""
    if p.kind == "pass":
        rec = await _pass_record(p.pass_id)
        if not rec or rec.get("revoked") or (rec.get("expires") and rec["expires"] < time.time()):
            return False
        p.admin = bool(rec.get("admin"))
        return True
    if p.kind == "google":
        access = await _access()
        env_admin = p.email in settings.admin_emails
        p.admin = env_admin or p.email in {a.lower() for a in access.get("admins", [])}
        ok, why = auth.check_access(p.email, p.hd, access)
        return env_admin or (ok and why != "blocked")
    return False


def _guarded(handler, admin: bool = False, write: bool = False):
    async def wrapped(request: Request):
        p = _principal(request)
        if p is None:
            return _json({"error": "Sign in first."}, 401)
        if not await _still_allowed(p):
            await _event("session-refused", request, p, via=p.kind)
            resp = _json({"error": "Your access to this playground has ended. Ask its owner."}, 401)
            resp.delete_cookie(auth.COOKIE, path="/")
            return resp
        if admin and not _is_admin(p):
            await _event("admin-denied", request, p, path=request.url.path)
            return _json({"error": "Owner only."}, 403)
        if write and request.headers.get(auth.CSRF_HEADER) != "1":
            return _json({"error": "Missing request header."}, 400)
        return await handler(request, p)
    return wrapped


def _is_admin(p: auth.Principal) -> bool:
    return bool(p.admin or (p.email and p.email.lower() in settings.admin_emails))


_TTL = 10.0
_cache: dict = {}


async def _access(fresh: bool = False) -> dict:
    """The access list, read at most every 10 s per instance (a copy, so callers may edit it)."""
    hit = _cache.get("access")
    if fresh or not hit or time.time() - hit[0] > _TTL:
        hit = (time.time(), await S.store.get_access())
        _cache["access"] = hit
    return copy.deepcopy(hit[1])


async def _pass_record(pid: str) -> dict | None:
    hit = _cache.get("pass:" + pid)
    if not hit or time.time() - hit[0] > _TTL:
        hit = (time.time(), await S.store.get_pass(pid) if pid else None)
        _cache["pass:" + pid] = hit
    return hit[1]


def _forget(key: str | None = None):
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def _writers(access: dict) -> list[dict]:
    off = set(access.get("writers_off", []))
    return [dict(w, enabled=w["enabled"] and w["code"] not in off) for w in public_writers()]


async def _body(request: Request, limit: int = 400_000) -> dict:
    raw = await request.body()
    if len(raw) > limit:
        raise ValueError("request too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("request is not JSON")
    if not isinstance(data, dict):
        raise ValueError("request is not a JSON object")
    return data


def _page(name: str, csp: str = PAGE_CSP) -> Response:
    return FileResponse(STATIC / name, headers={**SECURITY_HEADERS, "Content-Security-Policy": csp,
                                               "Cache-Control": "no-cache"})


# ---------------------------------------------------------------- pages and sign-in

async def _page_principal(request: Request) -> auth.Principal | None:
    p = _principal(request)
    return p if p is not None and await _still_allowed(p) else None


async def index(request: Request):
    p = await _page_principal(request)
    return _page("index.html" if p else "signin.html")


async def admin_page(request: Request):
    p = await _page_principal(request)
    if p is None or not _is_admin(p):
        return RedirectResponse("/", 303)
    return _page("admin.html")


async def viewer(request: Request):
    return _page("viewer.html", VIEWER_CSP)


def _set_session(resp: Response, data: dict):
    resp.set_cookie(auth.COOKIE, S.signer.cookie(data), max_age=auth.SESSION_SECONDS, httponly=True,
                    secure=settings.secure_cookies, samesite="lax", path="/")


async def signin_config(request: Request):
    access = await _access()
    return _json({"oauth_client_id": access.get("oauth_client_id") or settings.oauth_client_id})


async def pass_signin(request: Request):
    token = request.path_params["token"]
    pid = S.signer.read_pass_token(token)
    rec = await S.store.get_pass(pid) if pid else None
    now = time.time()
    if not rec or rec.get("revoked") or (rec.get("expires") and rec["expires"] < now):
        why = "unknown" if not rec else ("revoked" if rec.get("revoked") else "expired")
        await _event("pass-denied", request, None, who=(rec or {}).get("label", ""), reason=why,
                     pass_id=pid or "")
        return RedirectResponse("/?denied=pass", 303, headers=SECURITY_HEADERS)
    await S.store.update_pass(pid, {"uses": int(rec.get("uses", 0)) + 1, "last_used": now})
    data = {"id": f"p:{pid}", "kind": "pass", "display": rec.get("label", "pass"),
            "email": rec.get("email", ""), "domain": (rec.get("email", "").rsplit("@", 1)[-1]
                                                      if "@" in rec.get("email", "") else ""),
            "admin": bool(rec.get("admin")), "pass_id": pid,
            "per_hour": rec.get("per_hour"), "per_day": rec.get("per_day")}
    p = auth.principal_from_session(data)
    await S.store.touch_user(p.id, {"display": p.display, "kind": "pass"})
    await _event("signin", request, p, method="pass")
    resp = RedirectResponse("/", 303, headers=SECURITY_HEADERS)
    _set_session(resp, data)
    return resp


async def google_signin(request: Request):
    if request.headers.get(auth.CSRF_HEADER) != "1":
        return _json({"error": "Missing request header."}, 400)
    access = await _access()
    client_id = access.get("oauth_client_id") or settings.oauth_client_id
    if not client_id:
        return _json({"error": "Google sign-in is not set up yet. Use your pass link."}, 400)
    try:
        body = await _body(request, 20_000)
        claims = await asyncio.to_thread(auth.verify_google_token, str(body.get("credential", "")),
                                         client_id)
    except Exception as exc:
        await _event("signin-denied", request, None, reason=f"bad token: {str(exc)[:80]}")
        return _json({"error": "That sign-in could not be checked. Try again."}, 401)
    email = claims["email"].lower()
    allowed, why = auth.check_access(email, claims.get("hd", ""), access)
    is_admin = email in settings.admin_emails or email in {a.lower() for a in access.get("admins", [])}
    if not allowed and not is_admin:
        await _event("signin-denied", request, None, who=email, reason=why)
        return _json({"error": f"{email} is not on this playground's list. Ask its owner for a pass."}, 403)
    data = {"id": auth.email_id(email), "kind": "google", "display": email, "email": email,
            "domain": email.rsplit("@", 1)[-1], "hd": str(claims.get("hd", "")).lower()[:120],
            "admin": is_admin}
    p = auth.principal_from_session(data)
    await S.store.touch_user(p.id, {"display": email, "kind": "google", "rule": why if allowed else "admin"})
    await _event("signin", request, p, method="google", rule=why)
    resp = _json({"ok": True})
    _set_session(resp, data)
    return resp


async def signout(request: Request):
    resp = _json({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


# ---------------------------------------------------------------- the playground API

async def me(request: Request, p: auth.Principal):
    access = await _access()
    lim = effective_limits(settings.limits, access)
    snap = await S.guard.snapshot(p, auth.ip_key(_ip(request), S.signer), lim)
    return _json({"me": p.public(), "admin": _is_admin(p), "limits": snap,
                  "writers": _writers(access), "paused": bool(access.get("paused")),
                  "validator": settings.validator_commit, "xite": settings.xite_version,
                  "max_repairs": lim.max_repairs, "max_prompt_chars": lim.max_prompt_chars,
                  "mutations": mutate.catalogue()})


def _sse(ev: dict) -> bytes:
    return f"data: {json.dumps(ev, separators=(',', ':'))}\n\n".encode()


async def run(request: Request, p: auth.Principal):
    access = await _access()
    lim = effective_limits(settings.limits, access)
    try:
        body = await _body(request, lim.max_scene_chars + 20_000)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    prompt = str(body.get("prompt", "")).strip()
    source = str(body.get("x3d", ""))
    visibility = body.get("visibility") if isinstance(body.get("visibility"), dict) else None
    writer = str(body.get("writer", "a"))
    if len(prompt) > lim.max_prompt_chars:
        await _event("prompt-too-long", request, p, chars=len(prompt))
        return _json({"error": f"Prompts are limited to {lim.max_prompt_chars} characters."}, 413)
    if len(source) > lim.max_scene_chars:
        return _json({"error": f"Scenes are limited to {lim.max_scene_chars:,} characters."}, 413)
    if not prompt and not source:
        return _json({"error": "Type a prompt or paste a scene."}, 400)
    enabled = {w["code"] for w in _writers(access) if w["enabled"]}
    if writer not in enabled:
        return _json({"error": "That writer is switched off."}, 400)
    if visibility and not source:
        return _json({"error": "A visibility repair needs the scene."}, 400)
    req = RunRequest(prompt=prompt, writer=writer, judge=str(body.get("judge", "")),
                     max_repairs=max(0, min(int(body.get("max_repairs", 2) or 0), lim.max_repairs)),
                     autofix=bool(body.get("autofix", True)),
                     version="4.1" if str(body.get("version")) == "4.1" else "4.0",
                     source_x3d=source, visibility=visibility,
                     parent_run=str(body.get("parent_run", ""))[:20])
    ipk = auth.ip_key(_ip(request), S.signer)
    try:
        lease = await S.guard.admit_run(p, ipk, lim, access)
    except Denied as d:
        await _event("rate-limited", request, p, reason=d.reason)
        return _denied(d)

    async def charge(usd: float) -> bool:
        return await S.guard.charge(usd, lim)

    async def stream():
        holder: dict = {}
        deadline = time.time() + lim.run_timeout_s
        gen = run_loop(req, S.llm, settings, charge, holder=holder)
        try:
            yield b": open\n\n"
            async for ev in gen:
                if time.time() > deadline:
                    await gen.aclose()   # stops the models; the record keeps the last draft
                    yield _sse({"t": "error", "message": "the run took too long and was stopped"})
                    record = holder["record"]
                    yield _sse({"t": "final", "x3d": record.final_x3d, "verdict": record.final_verdict,
                                "summary": record.summary()})
                    break
                if ev["t"] == "_ping":
                    yield b": keep-alive\n\n"
                    continue
                if ev["t"] == "_record":
                    continue
                if ev["t"] == "final" and not holder.get("stored"):
                    # Stored before the page sees the scene, so the browser's
                    # verdict always has a run to land on, even while the judge
                    # is still answering.
                    await _store_run(holder["record"], p, first=True)
                    holder["stored"] = True
                yield _sse(ev)
        finally:
            # A closed tab cancels this response; the clean-up must still run,
            # or the person's one-run lease stays held until it expires.
            with anyio.CancelScope(shield=True):
                await gen.aclose()
                await S.store.release(lease)
                record = holder.get("record")
                if record is not None:
                    await _finish(record, p, stored=bool(holder.get("stored")))

    await _event("run", request, p, writer=writer, mode=("visible-repair" if visibility else
                                                           "paste" if source else "prompt"))
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={**SECURITY_HEADERS, "Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def _store_run(record, p: auth.Principal, *, first: bool) -> dict:
    s = record.summary()
    doc = {**s, "ts": record.started, "principal": p.id, "who": p.display, "prompt": record.prompt[:4000],
           "x3d": record.final_x3d[:200_000]}
    try:
        if first:
            await S.store.put_run(record.run_id, doc)
        else:   # merge: keeps the browser's verdict and thumbnail if they landed first
            await S.store.update_run(record.run_id, doc)
    except Exception as exc:
        log.warning("run store failed: %s", exc)
    return s


async def _finish(record, p: auth.Principal, *, stored: bool = False):
    s = await _store_run(record, p, first=not stored)
    try:
        await S.store.touch_user(p.id, {"display": p.display, "kind": p.kind}, runs=1)
    except Exception as exc:
        log.warning("user store failed: %s", exc)
    w = WRITERS.get(record.writer)
    j = WRITERS.get(record.judge) if record.judge else None
    await S.runlog.insert(row_for(record, p, w, j, settings.phase_label))
    log.info(json.dumps({"event": "run-done", "run": record.run_id, "writer": record.writer,
                         "first": s["first_verdict"], "final": s["final_verdict"], "usd": s["usd"],
                         "ms": s["ms"], "steps": len(s["steps"]), "stopped": s["stopped"]}))


async def validate(request: Request, p: auth.Principal):
    access = await _access()
    lim = effective_limits(settings.limits, access)
    try:
        body = await _body(request, lim.max_scene_chars + 10_000)
        await S.guard.admit_validate(p, auth.ip_key(_ip(request), S.signer), lim)
    except Denied as d:
        return _denied(d)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    text = str(body.get("x3d", ""))
    if not text.strip():
        return _json({"error": "Paste a scene first."}, 400)
    report = await asyncio.to_thread(full_report, text)
    return _json({"report": report, "validator": settings.validator_commit})


async def mutate_scene(request: Request, p: auth.Principal):
    try:
        body = await _body(request)
        out = mutate.mutate(str(body.get("x3d", "")), str(body.get("op", "")), int(body.get("seed", 0) or 0))
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    return _json(out)


async def visible(request: Request, p: auth.Principal):
    run_id = request.path_params["run_id"]
    try:
        body = await _body(request, 120_000)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    rec = await S.store.get_run(run_id)
    if not rec or (rec.get("principal") != p.id and not _is_admin(p)):
        return _json({"error": "No such run."}, 404)
    v = {k: body.get(k) for k in ("status", "coverage", "background", "bbox", "width", "height",
                                  "messages", "load_error", "sha256", "step", "xite", "ms")}
    v["ua"] = request.headers.get("user-agent", "")[:200]
    thumb = str(body.get("thumb", ""))
    patch = {"visible": v}
    if thumb.startswith("data:image/") and len(thumb) < 90_000:
        patch["thumb"] = thumb
    await S.store.update_run(run_id, patch)
    await S.runlog.insert(visible_row(run_id, settings.phase_label, p, v))
    return _json({"ok": True})


async def get_run(request: Request, p: auth.Principal):
    rec = await S.store.get_run(request.path_params["run_id"])
    if not rec or (rec.get("principal") != p.id and not _is_admin(p)):
        return _json({"error": "No such run."}, 404)
    rec.pop("principal", None)
    return _json(rec)


async def history(request: Request, p: auth.Principal):
    rows = await S.store.list_runs(20, principal=p.id)
    keep = ("run_id", "ts", "mode", "writer", "first_verdict", "final_verdict", "model_repairs",
            "validator_repairs", "usd", "ms", "prompt", "visible", "sha256")
    return _json({"runs": [{k: r.get(k) for k in keep} for r in rows]})


async def arena(request: Request, p: auth.Principal):
    return _json(S.arena)


# ---------------------------------------------------------------- admin

async def admin_overview(request: Request, p: auth.Principal):
    access = await _access()
    lim = effective_limits(settings.limits, access)
    snap = await S.guard.snapshot(p, "-", lim)
    runs = await S.store.list_runs(60)
    for r in runs:
        r.pop("x3d", None)
    passes = await S.store.list_passes()
    for ps in passes:
        ps["link"] = "/p/" + S.signer.pass_token(ps["id"])
    return _json({
        "access": access, "limits": lim.as_dict(), "counters": snap,
        "runs": runs, "events": await S.store.list_events(150), "passes": passes,
        "users": await S.store.list_users(200),
        "writers": [dict(w, judge_default=judge_for(w["code"]).code) for w in _writers(access)],
        "validator": settings.validator_commit, "xite": settings.xite_version,
        "env_admins": len(settings.admin_emails), "uptime_s": int(time.time() - S.started),
        "store": settings.store, "runlog": S.runlog.enabled})


def _clean_list(values, limit=500) -> list[str]:
    out = []
    for v in values or []:
        v = str(v).strip().lower()
        if v and len(v) < 200 and v not in out:
            out.append(v)
    return out[:limit]


async def admin_access(request: Request, p: auth.Principal):
    try:
        body = await _body(request, 200_000)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    access = await _access()
    for key in ("domains", "emails", "blocked", "admins", "writers_off"):
        if key in body:
            access[key] = _clean_list(body[key])
    if "oauth_client_id" in body:
        access["oauth_client_id"] = str(body["oauth_client_id"]).strip()[:200]
    if "paused" in body:
        access["paused"] = bool(body["paused"])
    if isinstance(body.get("limits"), dict):
        base = settings.limits.as_dict()
        access["limits"] = {k: v for k, v in body["limits"].items()
                            if k in base and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    await S.store.put_access(access, p.display)
    _forget("access")
    await _event("admin-change", request, p, keys=",".join(sorted(body.keys())))
    return _json({"ok": True, "access": await _access(fresh=True)})


async def admin_new_pass(request: Request, p: auth.Principal):
    try:
        body = await _body(request, 10_000)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    label = str(body.get("label", "")).strip()[:80]
    if not label:
        return _json({"error": "Give the pass a label (who it is for)."}, 400)
    days = float(body.get("days", 30) or 0)
    pid = auth.new_pass_id()

    def opt_int(name):
        v = body.get(name)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None

    rec = {"label": label, "email": str(body.get("email", "")).strip().lower()[:200],
           "created": time.time(), "created_by": p.display,
           "expires": time.time() + days * 86400 if days > 0 else 0, "revoked": False,
           "admin": bool(body.get("admin", False)), "per_hour": opt_int("per_hour"),
           "per_day": opt_int("per_day"), "uses": 0, "note": str(body.get("note", ""))[:300]}
    await S.store.put_pass(pid, rec)
    await _event("pass-created", request, p, label=label, admin=rec["admin"])
    return _json({"id": pid, "link": "/p/" + S.signer.pass_token(pid), **rec})


async def admin_revoke_pass(request: Request, p: auth.Principal):
    pid = request.path_params["pass_id"]
    if not await S.store.get_pass(pid):
        return _json({"error": "No such pass."}, 404)
    await S.store.update_pass(pid, {"revoked": True, "revoked_at": time.time(), "revoked_by": p.display})
    _forget("pass:" + pid)
    await _event("pass-revoked", request, p, pass_id=pid)
    return _json({"ok": True})


async def health(request: Request):
    return _json({"ok": True, "validator": settings.validator_commit, "xite": settings.xite_version,
                  "store": settings.store, "uptime_s": int(time.time() - S.started)})


class XiteFiles(StaticFiles):
    """X_ITE's own files, readable from the viewer's opaque origin."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        return resp


def _routes():
    routes = [
        Route("/", index),
        Route("/admin", admin_page),
        Route("/viewer", viewer),
        Route("/health", health),
        Route("/p/{token}", pass_signin),
        Route("/auth/config", signin_config),
        Route("/auth/google", google_signin, methods=["POST"]),
        Route("/auth/signout", signout, methods=["POST"]),
        Route("/api/me", _guarded(me)),
        Route("/api/run", _guarded(run, write=True), methods=["POST"]),
        Route("/api/validate", _guarded(validate, write=True), methods=["POST"]),
        Route("/api/mutate", _guarded(mutate_scene, write=True), methods=["POST"]),
        Route("/api/runs/{run_id}/visible", _guarded(visible, write=True), methods=["POST"]),
        Route("/api/runs/{run_id}", _guarded(get_run)),
        Route("/api/history", _guarded(history)),
        Route("/api/arena", _guarded(arena)),
        Route("/api/admin/overview", _guarded(admin_overview, admin=True)),
        Route("/api/admin/access", _guarded(admin_access, admin=True, write=True), methods=["PUT"]),
        Route("/api/admin/passes", _guarded(admin_new_pass, admin=True, write=True), methods=["POST"]),
        Route("/api/admin/passes/{pass_id}/revoke", _guarded(admin_revoke_pass, admin=True, write=True),
              methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ]
    if Path(settings.xite_dir).is_dir():
        routes.append(Mount("/xite", XiteFiles(directory=settings.xite_dir), name="xite"))
    return routes


app = Starlette(routes=_routes(), lifespan=lifespan)


def main():
    import uvicorn
    uvicorn.run("playground.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="info")


if __name__ == "__main__":
    main()
