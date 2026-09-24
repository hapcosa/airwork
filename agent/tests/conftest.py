import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolPermissionContext, ToolUseBlock,
)

from airwork_agent.app import create_app
from airwork_agent.config import Settings
from airwork_agent.projects import encode

SID_OLD = "11111111-1111-4111-8111-111111111111"
SID_WT = "22222222-2222-4222-8222-222222222222"


def _line(**kw):
    return json.dumps(kw, ensure_ascii=False) + "\n"


def write_session(path: Path, sid: str, cwd: str, title: str = "") -> None:
    lines = [
        _line(type="permission-mode", permissionMode="default", sessionId=sid),
        _line(type="user", uuid="u1", sessionId=sid, cwd=cwd, gitBranch="main", entrypoint="cli",
              timestamp="2026-09-01T10:00:00Z", message={"role": "user", "content": "hola, revisa el README"}),
        _line(type="assistant", uuid="a1", sessionId=sid, cwd=cwd,
              message={"role": "assistant", "model": "claude-sonnet-5", "content": [
                  {"type": "thinking", "thinking": "pensando"},
                  {"type": "text", "text": "Voy a leerlo."},
                  {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "README.md"}}]}),
        _line(type="user", uuid="u2", sessionId=sid, cwd=cwd, toolUseResult={},
              message={"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5000}]}),
        _line(type="user", uuid="u3", sessionId=sid, cwd=cwd, isMeta=True,
              message={"role": "user", "content": "meta que no se muestra"}),
        _line(type="user", uuid="u4", sessionId=sid, cwd=cwd,
              message={"role": "user", "content": "<command-name>/compact</command-name>"}),
        _line(type="system", subtype="compact_boundary", uuid="s1", sessionId=sid,
              compactMetadata={"trigger": "manual", "preTokens": 1000}),
        _line(type="assistant", uuid="a2", sessionId=sid, cwd=cwd, isSidechain=True,
              message={"role": "assistant", "content": [{"type": "text", "text": "subagente"}]}),
        _line(type="ai-title", aiTitle="Revisión del README", sessionId=sid),
        _line(type="last-prompt", lastPrompt="hola, revisa el README", sessionId=sid),
    ]
    if title:
        lines.append(_line(type="custom-title", customTitle=title, sessionId=sid))
    path.write_text("".join(lines))


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def make_env(tmp_path: Path) -> Settings:
    """Home falso con proyectos, historial y perfiles; también lo usa la prueba e2e de la PWA."""
    home = tmp_path / "home"
    prog = home / "programacion"
    proj = prog / "demo"
    (proj / ".git").mkdir(parents=True)
    (proj / "docs" / "handoff").mkdir(parents=True)
    (proj / "docs" / "handoff" / "2026-09-24-1200-demo.md").write_text("# Handoff\nprompt listo")
    wt = proj / ".claude" / "worktrees" / "feat"
    wt.mkdir(parents=True)
    (prog / "otro").mkdir()  # sin git ni historial: no se lista
    cp = home / ".claude" / "projects"
    d_main = cp / encode(proj)
    d_wt = cp / encode(wt)
    d_main.mkdir(parents=True)
    d_wt.mkdir(parents=True)
    write_session(d_main / f"{SID_OLD}.jsonl", SID_OLD, str(proj))
    write_session(d_wt / f"{SID_WT}.jsonl", SID_WT, str(wt), title="Título propio")
    old = time.time() - 3600
    for f in (d_main / f"{SID_OLD}.jsonl", d_wt / f"{SID_WT}.jsonl"):
        os.utime(f, (old, old))
    for alias, settings in (("cuenta1", {"env": {}}), ("cuenta2", {}), ("mala", {"env": {"ANTHROPIC_BASE_URL": "x"}})):
        d = home / ".claude-accounts" / alias
        d.mkdir(parents=True)
        (d / "settings.json").write_text(json.dumps(settings))
    (home / ".claude-accounts" / "cuenta1" / ".credentials.json").write_text("{}")
    return Settings(home=home, dev=True, claude_bin="/bin/true", permission_timeout_s=5)


class FakeClient:
    """Imita ClaudeSDKClient: responde a cada query con init/text/result y pide permiso si el prompt dice PERMISO."""

    instances: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queries: list[str] = []
        self.model = options.model
        self.mode = options.permission_mode
        self.interrupted = False
        self.connected = False
        self.initialized = False
        FakeClient.instances.append(self)

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        asyncio.get_running_loop().create_task(self._answer(prompt))

    async def _answer(self, prompt):
        sid = self.options.session_id or self.options.resume
        if self.options.fork_session:
            sid = "33333333-3333-4333-8333-333333333333"
        if not self.initialized:
            self.initialized = True
            await self.queue.put(SystemMessage("init", {"session_id": sid, "model": self.model or "claude-sonnet-5",
                                                        "permissionMode": self.mode, "cwd": str(self.options.cwd)}))
        if "PERMISO" in prompt:
            await self.queue.put(AssistantMessage(content=[ToolUseBlock("t9", "Bash", {"command": "ls"})],
                                                  model="claude-sonnet-5"))
            res = await self.options.can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext())
            text = f"permiso:{res.behavior}"
        else:
            text = f"eco:{prompt}"
        await self.queue.put(AssistantMessage(content=[TextBlock(text)], model="claude-sonnet-5"))
        await self.queue.put(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                           num_turns=1, session_id=sid, result=text))

    async def receive_messages(self):
        while True:
            msg = await self.queue.get()
            if msg is None:
                return
            yield msg

    async def interrupt(self):
        self.interrupted = True

    async def set_model(self, model=None):
        self.model = model

    async def set_permission_mode(self, mode):
        self.mode = mode

    async def get_context_usage(self):
        return {"totalTokens": 1234, "maxTokens": 200000, "percentage": 0.6, "model": "claude-sonnet-5",
                "categories": [], "gridRows": [[]]}

    async def disconnect(self):
        self.connected = False
        await self.queue.put(None)


@pytest.fixture
def app(env):
    FakeClient.instances.clear()
    return create_app(env, client_factory=FakeClient)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as c:
        yield c
    await app.state.runs.close_all("fin del test")
    app.state.store.close()


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condición no cumplida a tiempo")


@pytest.fixture
def store(env):
    from airwork_agent.store import Store
    st = Store(env)
    yield st
    st.close()
