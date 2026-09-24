"""Procesos de Claude Code (vía Agent SDK): uno por sesión activa, con buffer de eventos y permisos remotos."""
from __future__ import annotations

import asyncio
import collections
import itertools
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny,
    RateLimitEvent, ResultMessage, StreamEvent, SystemMessage, TextBlock, ThinkingBlock, ToolPermissionContext,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from .audit import redact
from .config import Settings
from .history import TOOL_INPUT_MAX, TOOL_RESULT_MAX, _tool_result_text, _truncate
from .store import Store

log = logging.getLogger("airwork.runs")

BUFFER_SIZE = 2000
EXTERNAL_WRITE_WINDOW_S = 90
# Fragmentos de error que indican cuota agotada o credenciales inválidas.
ACCOUNT_ERROR_HINTS = ("rate limit", "usage limit", "session limit", "weekly limit", "quota", "limit reached",
                       "authentication", "unauthorized", "oauth", "invalid api key", "credit")
ACCOUNT_ERROR_STATUS = (401, 403, 429)


class RunError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


@dataclass
class PendingPermission:
    req_id: str
    tool: str
    future: asyncio.Future
    created: float = field(default_factory=time.time)


@dataclass
class RunConfig:
    project: str
    cwd: str
    account_id: int
    session_id: str | None = None  # None = sesión nueva
    fork: bool = False
    model: str | None = None
    effort: str | None = None
    permission_mode: str = "default"


ClientFactory = Callable[[ClaudeAgentOptions], Any]


class Run:
    def __init__(self, manager: "RunManager", cfg: RunConfig):
        self.m = manager
        self.cfg = cfg
        self.id = uuid.uuid4().hex[:12]
        self.session_id: str | None = None if cfg.fork else cfg.session_id
        self.state = "starting"  # starting | idle | busy | closed
        self.created = self.last_activity = time.time()
        self.events: collections.deque[dict] = collections.deque(maxlen=BUFFER_SIZE)
        self._seq = itertools.count(1)
        self._cond = asyncio.Condition()
        self.pending: dict[str, PendingPermission] = {}
        self.session_allow: set[str] = set()
        self.client: Any = None
        self._reader: asyncio.Task | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

    # --- eventos ----------------------------------------------------------------

    async def emit(self, kind: str, **data: Any) -> None:
        ev = {"seq": next(self._seq), "kind": kind, "ts": time.time(), **data}
        async with self._cond:
            self.events.append(ev)
            self._cond.notify_all()

    async def subscribe(self, after: int = 0):
        """Genera eventos con seq > after; termina cuando el run se cierra."""
        last = after
        while True:
            async with self._cond:
                pending = [e for e in self.events if e["seq"] > last]
                if not pending:
                    if self.state == "closed":
                        return
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=15)
                    except TimeoutError:
                        yield None  # keepalive
                        continue
                    pending = [e for e in self.events if e["seq"] > last]
            for e in pending:
                last = e["seq"]
                yield e
            if self.state == "closed" and not any(e["seq"] > last for e in self.events):
                return

    def public(self) -> dict:
        return {
            "run_id": self.id, "session_id": self.session_id, "project": self.cfg.project, "cwd": self.cfg.cwd,
            "account_id": self.cfg.account_id, "model": self.cfg.model, "effort": self.cfg.effort,
            "permission_mode": self.cfg.permission_mode, "state": self.state, "created": self.created,
            "last_activity": self.last_activity,
            "pending_permissions": [{"req_id": p.req_id, "tool": p.tool} for p in self.pending.values()],
            "last_seq": self.events[-1]["seq"] if self.events else 0,
        }

    # --- permisos -----------------------------------------------------------------

    async def can_use_tool(self, tool: str, tool_input: dict, ctx: ToolPermissionContext):
        if tool in self.session_allow:
            return PermissionResultAllow()
        req = PendingPermission(uuid.uuid4().hex[:16], tool, asyncio.get_running_loop().create_future())
        self.pending[req.req_id] = req
        raw = json.dumps(tool_input, ensure_ascii=False, default=str)
        text, cut = _truncate(raw, TOOL_INPUT_MAX)
        await self.emit("permission_request", req_id=req.req_id, tool=tool, input=text, truncated=cut,
                        title=getattr(ctx, "title", None), description=getattr(ctx, "description", None),
                        decision_reason=getattr(ctx, "decision_reason", None),
                        blocked_path=getattr(ctx, "blocked_path", None))
        self.last_activity = time.time()
        try:
            decision, message, scope = await asyncio.wait_for(req.future, timeout=self.m.s.permission_timeout_s)
        except TimeoutError:
            decision, message, scope = "deny", "Sin respuesta desde el celular (timeout)", "once"
        finally:
            self.pending.pop(req.req_id, None)
        await self.emit("permission_resolved", req_id=req.req_id, decision=decision, scope=scope)
        if decision == "allow":
            if scope == "session":
                self.session_allow.add(tool)
            return PermissionResultAllow()
        return PermissionResultDeny(message=message or "Denegado desde el celular")

    def resolve_permission(self, req_id: str, decision: str, message: str | None, scope: str) -> bool:
        req = self.pending.get(req_id)
        if req is None or req.future.done():
            return False
        req.future.set_result((decision, message, scope))
        return True

    # --- ciclo de vida ------------------------------------------------------------

    def _options(self, env: dict[str, str]) -> ClaudeAgentOptions:
        c = self.cfg
        opts = ClaudeAgentOptions(
            cwd=c.cwd, cli_path=self.m.s.claude_bin, env=env, model=c.model, effort=c.effort,  # type: ignore[arg-type]
            permission_mode=c.permission_mode,  # type: ignore[arg-type]
            can_use_tool=self.can_use_tool, stderr=self._on_stderr,
        )
        if c.session_id:
            opts.resume = c.session_id
            opts.fork_session = c.fork
        else:
            self.session_id = str(uuid.uuid4())
            opts.session_id = self.session_id
        return opts

    def _on_stderr(self, line: str) -> None:
        line = redact(line.rstrip())
        if line:
            self._stderr_tail.append(line)
            log.debug("claude[%s]: %s", self.id, line)

    async def start(self, prompt: str) -> None:
        env = self.m.store.child_env(self.cfg.account_id)
        self.client = self.m.client_factory(self._options(env))
        try:
            await self.client.connect()
        except Exception as e:
            self.state = "closed"
            await self.emit("error", text=redact(f"No se pudo iniciar claude: {e}"), stderr=list(self._stderr_tail))
            raise RunError(502, f"No se pudo iniciar claude: {redact(str(e))}") from None
        self._reader = asyncio.create_task(self._read_loop(), name=f"run-{self.id}")
        await self.send(prompt)

    async def send(self, prompt: str) -> None:
        if self.state == "closed":
            raise RunError(409, "El run está cerrado")
        self.state = "busy"
        self.last_activity = time.time()
        await self.emit("prompt", text=prompt)
        await self.client.query(prompt)

    async def interrupt(self) -> None:
        if self.state != "closed":
            await self.client.interrupt()

    async def close(self, reason: str = "cerrado") -> None:
        if self.state == "closed":
            return
        self.state = "closed"
        for req in list(self.pending.values()):
            if not req.future.done():
                req.future.set_result(("deny", "El run se cerró", "once"))
        if self._reader and self._reader is not asyncio.current_task():
            self._reader.cancel()
        try:
            await asyncio.wait_for(self.client.disconnect(), timeout=10)
        except Exception as e:  # noqa: BLE001 - el cierre no debe fallar
            log.warning("run %s: error al desconectar: %s", self.id, redact(str(e)))
        await self.emit("closed", reason=reason)
        self.m._forget(self)

    async def _read_loop(self) -> None:
        try:
            async for msg in self.client.receive_messages():
                self.last_activity = time.time()
                await self._handle(msg)
            await self.close("el proceso terminó")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            await self.emit("error", text=redact(f"{type(e).__name__}: {e}"), stderr=list(self._stderr_tail))
            await self.close("error")

    async def _handle(self, msg: Any) -> None:
        if isinstance(msg, SystemMessage):
            d = msg.data or {}
            if msg.subtype == "init":
                self.session_id = d.get("session_id") or self.session_id
                self.m._bind_session(self)
                if self.state == "starting":
                    self.state = "busy"
                await self.emit("init", session_id=self.session_id, model=d.get("model"),
                                permission_mode=d.get("permissionMode"), cwd=d.get("cwd"))
            elif msg.subtype == "compact_boundary":
                meta = d.get("compact_metadata") or {}
                await self.emit("compact", trigger=meta.get("trigger"), pre_tokens=meta.get("pre_tokens"),
                                post_tokens=meta.get("post_tokens"))
            elif msg.subtype == "status":
                await self.emit("status", status=d.get("status"), compact_result=d.get("compact_result"))
        elif isinstance(msg, AssistantMessage):
            if msg.error:
                await self.emit("error", text=str(msg.error))
            for b in msg.content:
                if isinstance(b, TextBlock):
                    await self.emit("text", text=b.text, model=msg.model, sidechain=msg.parent_tool_use_id is not None)
                elif isinstance(b, ThinkingBlock):
                    await self.emit("thinking", text=b.thinking)
                elif isinstance(b, ToolUseBlock):
                    raw = json.dumps(b.input, ensure_ascii=False, default=str)
                    text, cut = _truncate(raw, TOOL_INPUT_MAX)
                    await self.emit("tool_use", id=b.id, name=b.name, input=text, truncated=cut,
                                    sidechain=msg.parent_tool_use_id is not None)
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for b in content:
                if isinstance(b, ToolResultBlock):
                    text, cut = _truncate(_tool_result_text(b.content), TOOL_RESULT_MAX)
                    await self.emit("tool_result", tool_use_id=b.tool_use_id, is_error=bool(b.is_error),
                                    text=text, truncated=cut)
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            raw = getattr(info, "raw", None) or {}
            await self.emit("rate_limit", status=getattr(info, "status", None),
                            rate_limit_type=getattr(info, "rate_limit_type", None),
                            utilization=getattr(info, "utilization", None),
                            resets_at=getattr(info, "resets_at", None), windows=raw.get("unifiedWindows"))
            if getattr(info, "status", None) == "rejected":
                kind = getattr(info, "rate_limit_type", None) or "desconocida"
                self.m.store.record_error(self.cfg.account_id, f"Límite de uso agotado (ventana {kind}); "
                                          f"se reinicia en {getattr(info, 'resets_at', None)}")
        elif isinstance(msg, ResultMessage):
            self.state = "idle"
            await self.emit("result", subtype=msg.subtype, is_error=msg.is_error, result=msg.result,
                            num_turns=msg.num_turns, total_cost_usd=msg.total_cost_usd, usage=msg.usage,
                            api_error_status=msg.api_error_status, errors=msg.errors,
                            terminal_reason=msg.terminal_reason)
            if msg.is_error:
                detail = " ".join(str(x) for x in (msg.result, msg.errors, msg.api_error_status) if x)
                if msg.api_error_status in ACCOUNT_ERROR_STATUS or any(h in detail.lower() for h in ACCOUNT_ERROR_HINTS):
                    self.m.store.record_error(self.cfg.account_id, redact(detail)[:500])
            else:
                self.m.store.record_error(self.cfg.account_id, None)
        elif isinstance(msg, StreamEvent):
            return


def external_activity(session_id: str, path: Path | None, own_pids: set[int],
                      own_closed_at: float | None = None) -> list[str]:
    """Señales de que otra instancia de claude usa la sesión. Heurística (ver diseño §4.3).

    own_closed_at: cuándo el agente cerró su propio proceso de esta sesión; escrituras hasta
    ese momento son nuestras y no cuentan como actividad externa.
    """
    reasons = []
    proc = Path("/proc")
    if proc.is_dir():
        for p in proc.iterdir():
            if not p.name.isdigit() or int(p.name) in own_pids:
                continue
            try:
                args = (p / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if any(b"claude" in a for a in args[:2]) and any(session_id.encode() in a for a in args):
                reasons.append(f"proceso {p.name}")
    if path is not None and path.exists() and time.time() - path.stat().st_mtime < EXTERNAL_WRITE_WINDOW_S \
            and not (own_closed_at is not None and path.stat().st_mtime <= own_closed_at + 2):
        reasons.append("el historial cambió hace menos de 90 s")
    return reasons


class RunManager:
    def __init__(self, s: Settings, store: Store, client_factory: ClientFactory | None = None):
        self.s = s
        self.store = store
        self.client_factory: ClientFactory = client_factory or ClaudeSDKClient
        self.runs: dict[str, Run] = {}
        self.by_session: dict[str, str] = {}
        self.closed_at: dict[str, float] = {}  # session_id -> cierre de nuestro último proceso
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    def start_reaper(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop(), name="airwork-reaper")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for run in list(self.runs.values()):
                if run.state == "idle" and not run.pending and now - run.last_activity > self.s.idle_timeout_s:
                    await run.close("inactivo")

    def get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise RunError(404, "Run inexistente")
        return run

    def for_session(self, session_id: str) -> Run | None:
        rid = self.by_session.get(session_id)
        return self.runs.get(rid) if rid else None

    def _bind_session(self, run: Run) -> None:
        if run.session_id:
            self.by_session[run.session_id] = run.id

    def _forget(self, run: Run) -> None:
        self.runs.pop(run.id, None)
        if run.session_id and self.by_session.get(run.session_id) == run.id:
            self.by_session.pop(run.session_id, None)
            self.closed_at[run.session_id] = time.time()

    def _own_pids(self) -> set[int]:
        pids = {os.getpid()}
        for run in self.runs.values():
            proc = getattr(getattr(getattr(run.client, "_transport", None), "_process", None), "pid", None)
            if proc:
                pids.add(proc)
        return pids

    async def start(self, cfg: RunConfig, prompt: str, session_path: Path | None = None) -> tuple[Run, bool]:
        """Devuelve (run, reutilizado). Si la sesión ya está abierta en el agente, reutiliza su proceso."""
        async with self._lock:
            if cfg.session_id and not cfg.fork:
                existing = self.for_session(cfg.session_id)
                if existing is not None:
                    await existing.send(prompt)
                    return existing, True
                reasons = external_activity(cfg.session_id, session_path, self._own_pids(),
                                            self.closed_at.get(cfg.session_id))
                if reasons:
                    raise RunError(409, "La sesión parece abierta en otro proceso; usa fork", reasons=reasons)
            if len(self.runs) >= self.s.max_runs:
                raise RunError(429, f"Máximo de {self.s.max_runs} sesiones activas; cierra alguna")
            run = Run(self, cfg)
            self.runs[run.id] = run
            self._bind_session(run)
        try:
            await run.start(prompt)
        except Exception:
            self._forget(run)
            raise
        return run, False

    async def close_all(self, reason: str = "kill switch") -> int:
        runs = list(self.runs.values())
        for run in runs:
            await run.close(reason)
        return len(runs)

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await self.close_all("apagado del agente")

    async def context_usage(self, cfg: RunConfig) -> dict:
        """Uso de contexto de una sesión. Usa el proceso activo o abre uno temporal sin prompt."""
        run = self.for_session(cfg.session_id) if cfg.session_id else None
        if run is not None:
            return await run.client.get_context_usage()
        tmp = Run(self, cfg)
        client = self.client_factory(tmp._options(self.store.child_env(cfg.account_id)))
        await client.connect()
        try:
            return await client.get_context_usage()
        finally:
            await client.disconnect()
