"""API HTTP de airwork-agent (FastAPI)."""
from __future__ import annotations

import asyncio
import json
import re
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, history, projects
from .audit import Audit, install_redaction
from .auth import AccessVerifier, Identity, check_origin
from .config import Settings, scrub_process_env
from .runs import ClientFactory, RunConfig, RunError, RunManager
from .store import InvalidInput, Store, validate_run_options

HANDOFF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.md$")
PWA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "pwa"


# --- cuerpos de petición ---------------------------------------------------------

class RunCreate(BaseModel):
    project: str
    prompt: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None
    fork: bool = False
    account_id: int | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None


class RunMessage(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class RunPatch(BaseModel):
    model: str | None = None
    permission_mode: str | None = None


class PermissionAnswer(BaseModel):
    decision: Literal["allow", "deny"]
    message: str | None = Field(default=None, max_length=2000)
    scope: Literal["once", "session"] = "once"


class AccountCreate(BaseModel):
    alias: str
    label: str = ""
    use_headroom: bool = False
    token: str | None = None


class AccountPatch(BaseModel):
    label: str | None = None
    use_headroom: bool | None = None
    token: str | None = None
    clear_token: bool = False


class Prefs(BaseModel):
    account_id: int | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None


# --- aplicación ----------------------------------------------------------------------

def _claude_version(bin_: str) -> str:
    try:
        out = subprocess.run([bin_, "--version"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip().split(" ")[0]
    except (OSError, subprocess.SubprocessError):
        return "desconocida"


def _port_open(url: str) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=0.3):
            return True
    except OSError:
        return False


def create_app(s: Settings | None = None, client_factory: ClientFactory | None = None) -> FastAPI:
    s = s or Settings.from_env()
    scrub_process_env()
    install_redaction()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.claude_version = await asyncio.to_thread(_claude_version, s.claude_bin)
        runs.start_reaper()
        yield
        await runs.shutdown()
        store.close()

    app = FastAPI(title="airwork-agent", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    store = Store(s)
    runs = RunManager(s, store, client_factory)
    audit = Audit(s.audit_path)
    verifier = AccessVerifier(s)
    app.state.settings, app.state.store, app.state.runs, app.state.audit = s, store, runs, audit
    app.state.claude_version = "desconocida"

    @app.exception_handler(RunError)
    async def _run_error(_: Request, e: RunError):
        return JSONResponse({"detail": e.message, **e.extra}, status_code=e.status)

    @app.exception_handler(InvalidInput)
    async def _invalid(_: Request, e: InvalidInput):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def ident(request: Request) -> Identity:
        who = verifier.identify(request)
        check_origin(request)
        return who

    def project_path(name: str) -> Path:
        try:
            return projects.resolve_project(s, name)
        except projects.NotFound:
            raise HTTPException(404, "Proyecto inexistente") from None

    def session_path(session_id: str) -> Path:
        p = history.find_session(s, session_id)
        if p is None:
            raise HTTPException(404, "Sesión inexistente")
        return p

    # --- lectura -------------------------------------------------------------------

    @app.get("/api/pc")
    def pc(who: Identity = Depends(ident)):
        return {"name": s.pc_name, "agent_version": __version__, "claude_version": app.state.claude_version,
                "active_runs": len(runs.runs), "max_runs": s.max_runs, "headroom_up": _port_open(s.headroom_url),
                "allow_tokens": s.allow_tokens, "email": who.email}

    @app.get("/api/projects")
    def list_projects(who: Identity = Depends(ident)):
        return projects.list_projects(s)

    @app.get("/api/projects/{project}/sessions")
    def list_sessions(project: str, limit: int = Query(50, ge=1, le=500), before: float | None = None,
                      who: Identity = Depends(ident)):
        rows = history.list_sessions(s, project_path(project), limit, before)
        for r in rows:
            run = runs.for_session(r["id"])
            r["active_run"] = run.id if run else None
        return rows

    @app.get("/api/sessions/{session_id}")
    def read_session(session_id: str, offset: int | None = Query(None, ge=0), limit: int = Query(200, ge=1, le=1000),
                     sidechain: bool = False, who: Identity = Depends(ident)):
        p = session_path(session_id)
        run = runs.for_session(session_id)
        return {"summary": history.summarize(p), "project": history.project_of(s, p),
                "active_run": run.id if run else None, **history.read_events(p, offset, limit, sidechain)}

    @app.get("/api/sessions/{session_id}/context")
    async def session_context(session_id: str, account_id: int | None = None, who: Identity = Depends(ident)):
        p = session_path(session_id)
        project = history.project_of(s, p) or ""
        cfg = _run_config(project, session_id, p, account_id, None, None, None, fork=False)
        usage = await runs.context_usage(cfg)
        keep = ("totalTokens", "maxTokens", "percentage", "model", "isAutoCompactEnabled", "autoCompactThreshold",
                "categories")
        return {k: usage.get(k) for k in keep if k in usage}

    @app.get("/api/projects/{project}/handoffs")
    def handoffs(project: str, name: str | None = None, who: Identity = Depends(ident)):
        d = project_path(project) / "docs" / "handoff"
        if name is None:
            if not d.is_dir():
                return []
            files = sorted((f for f in d.glob("*.md") if f.is_file()), key=lambda f: f.name, reverse=True)
            return [{"name": f.name, "path": f"docs/handoff/{f.name}", "mtime": f.stat().st_mtime} for f in files]
        if not HANDOFF_RE.match(name):
            raise HTTPException(400, "Nombre inválido")
        f = d / name
        if not f.is_file() or not projects.is_within(f, d):
            raise HTTPException(404, "Handoff inexistente")
        return {"name": name, "path": f"docs/handoff/{name}", "content": f.read_text(errors="replace")[:200_000]}

    # --- runs ------------------------------------------------------------------------

    def _run_config(project: str, session_id: str | None, p: Path | None, account_id: int | None,
                    model: str | None, effort: str | None, mode: str | None, fork: bool) -> RunConfig:
        proj = project_path(project)
        if p is not None:
            if history.project_of(s, p) != project:
                raise HTTPException(400, "La sesión no pertenece a ese proyecto")
            cwd = history.session_cwd(p)
            if not cwd or not projects.allowed_cwd(s, cwd):
                raise HTTPException(409, f"El directorio original de la sesión no existe o está fuera de la raíz: {cwd}")
        else:
            cwd = str(proj)
        prefs = store.effective_prefs(project, session_id)
        account_id = account_id if account_id is not None else prefs["account_id"]
        if account_id is None:
            raise HTTPException(400, "Elige una cuenta para esta conversación")
        if store.get_account(account_id) is None:
            raise HTTPException(400, "Cuenta inexistente")
        model = model or prefs["model"]
        effort = effort or prefs["effort"]
        mode = mode or prefs["permission_mode"] or "default"
        validate_run_options(model, effort, mode)
        return RunConfig(project=project, cwd=cwd, account_id=account_id, session_id=session_id, fork=fork,
                         model=model, effort=effort, permission_mode=mode)

    @app.post("/api/runs")
    async def create_run(body: RunCreate, who: Identity = Depends(ident)):
        p = session_path(body.session_id) if body.session_id else None
        cfg = _run_config(body.project, body.session_id, p, body.account_id, body.model, body.effort,
                          body.permission_mode, body.fork)
        try:
            run, reused = await runs.start(cfg, body.prompt, p)
        except RunError as e:
            audit.record(who.email, who.ip, "run.create", f"error {e.status}", project=body.project,
                         session=body.session_id, account=cfg.account_id)
            raise
        if run.session_id and not reused:
            store.set_prefs("session", run.session_id, account_id=cfg.account_id, model=cfg.model,
                            effort=cfg.effort, permission_mode=cfg.permission_mode)
        audit.record(who.email, who.ip, "run.create", project=body.project, session=run.session_id,
                     account=cfg.account_id, fork=body.fork, reused=reused, run=run.id)
        return {**run.public(), "reused": reused}

    @app.get("/api/runs")
    def list_runs(who: Identity = Depends(ident)):
        return [r.public() for r in runs.runs.values()]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, who: Identity = Depends(ident)):
        return runs.get(run_id).public()

    @app.post("/api/runs/{run_id}/messages")
    async def run_message(run_id: str, body: RunMessage, who: Identity = Depends(ident)):
        run = runs.get(run_id)
        await run.send(body.prompt)
        audit.record(who.email, who.ip, "run.message", run=run_id, session=run.session_id,
                     command=body.prompt.split()[0] if body.prompt.startswith("/") else None)
        return run.public()

    @app.post("/api/runs/{run_id}/interrupt")
    async def run_interrupt(run_id: str, who: Identity = Depends(ident)):
        run = runs.get(run_id)
        await run.interrupt()
        audit.record(who.email, who.ip, "run.interrupt", run=run_id, session=run.session_id)
        return run.public()

    @app.patch("/api/runs/{run_id}")
    async def run_patch(run_id: str, body: RunPatch, who: Identity = Depends(ident)):
        run = runs.get(run_id)
        validate_run_options(body.model, None, body.permission_mode)
        if body.model:
            await run.client.set_model(body.model)
            run.cfg.model = body.model
        if body.permission_mode:
            await run.client.set_permission_mode(body.permission_mode)
            run.cfg.permission_mode = body.permission_mode
        if run.session_id:
            store.set_prefs("session", run.session_id, account_id=run.cfg.account_id, model=run.cfg.model,
                            effort=run.cfg.effort, permission_mode=run.cfg.permission_mode)
        audit.record(who.email, who.ip, "run.patch", run=run_id, model=body.model, mode=body.permission_mode)
        return run.public()

    @app.delete("/api/runs/{run_id}")
    async def run_close(run_id: str, who: Identity = Depends(ident)):
        run = runs.get(run_id)
        await run.close("cerrado desde el celular")
        audit.record(who.email, who.ip, "run.close", run=run_id, session=run.session_id)
        return {"closed": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = Query(0, ge=0), who: Identity = Depends(ident)):
        run = runs.get(run_id)
        last = request.headers.get("last-event-id")
        start = int(last) if last and last.isdigit() else after

        async def stream():
            yield "retry: 3000\n\n"
            async for ev in run.subscribe(start):
                if await request.is_disconnected():
                    return
                if ev is None:
                    yield ": ping\n\n"
                    continue
                data = json.dumps(ev, ensure_ascii=False, default=str)
                yield f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {data}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/permissions/{req_id}")
    async def answer_permission(req_id: str, body: PermissionAnswer, who: Identity = Depends(ident)):
        for run in runs.runs.values():
            req = run.pending.get(req_id)
            if req is not None and run.resolve_permission(req_id, body.decision, body.message, body.scope):
                audit.record(who.email, who.ip, "permission", run=run.id, session=run.session_id, tool=req.tool,
                             decision=body.decision, scope=body.scope)
                return {"ok": True}
        raise HTTPException(404, "Solicitud de permiso inexistente o ya resuelta")

    @app.post("/api/kill")
    async def kill(who: Identity = Depends(ident)):
        n = await runs.close_all()
        audit.record(who.email, who.ip, "kill", closed=n)
        return {"closed": n}

    # --- cuentas y preferencias ----------------------------------------------------

    @app.get("/api/accounts")
    def accounts(who: Identity = Depends(ident)):
        return store.list_accounts()

    @app.post("/api/accounts", status_code=201)
    def create_account(body: AccountCreate, who: Identity = Depends(ident)):
        acc = store.create_account(body.alias, body.label, body.use_headroom, body.token)
        audit.record(who.email, who.ip, "account.create", account=acc["id"], alias=acc["alias"],
                     token=body.token is not None)
        return acc

    @app.patch("/api/accounts/{account_id}")
    def patch_account(account_id: int, body: AccountPatch, who: Identity = Depends(ident)):
        acc = store.update_account(account_id, label=body.label, use_headroom=body.use_headroom, token=body.token,
                                   clear_token=body.clear_token)
        if acc is None:
            raise HTTPException(404, "Cuenta inexistente")
        audit.record(who.email, who.ip, "account.update", account=account_id, headroom=body.use_headroom,
                     token_changed=body.token is not None or body.clear_token)
        return acc

    @app.delete("/api/accounts/{account_id}")
    def delete_account(account_id: int, who: Identity = Depends(ident)):
        if any(r.cfg.account_id == account_id for r in runs.runs.values()):
            raise HTTPException(409, "La cuenta tiene sesiones activas")
        if not store.delete_account(account_id):
            raise HTTPException(404, "Cuenta inexistente")
        audit.record(who.email, who.ip, "account.delete", account=account_id)
        return {"deleted": True}

    @app.get("/api/prefs/{scope}/{key}")
    def get_prefs(scope: Literal["project", "session"], key: str, who: Identity = Depends(ident)):
        return store.get_prefs(scope, key)

    @app.put("/api/prefs/{scope}/{key}")
    def put_prefs(scope: Literal["project", "session"], key: str, body: Prefs, who: Identity = Depends(ident)):
        if scope == "project":
            project_path(key)
        elif not history.UUID_RE.match(key):
            raise HTTPException(400, "session_id inválido")
        out = store.set_prefs(scope, key, **body.model_dump())
        audit.record(who.email, who.ip, "prefs", scope=scope, key=key, **body.model_dump())
        return out

    if PWA_DIR.is_dir():
        app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")

    return app
