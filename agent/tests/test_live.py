"""Prueba contra el `claude` real. Gasta cuota (haiku, pocos tokens). Ejecutar con:
AIRWORK_LIVE_ACCOUNT=<alias> uv run pytest -m live
"""
import os
import shutil
from pathlib import Path

import httpx
import pytest

from airwork_agent.app import create_app
from airwork_agent.config import Settings
from airwork_agent.projects import encode
from conftest import wait_for

pytestmark = [pytest.mark.live, pytest.mark.skipif(not os.environ.get("AIRWORK_LIVE_ACCOUNT"),
                                                   reason="falta AIRWORK_LIVE_ACCOUNT")]
H = {"Origin": "http://agent.test"}


@pytest.fixture
async def live(tmp_path):
    root = tmp_path / "prog"
    (root / "vivo" / ".git").mkdir(parents=True)
    s = Settings(programming_root=root, data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                 config_dir=tmp_path / "cfg", dev=True, permission_timeout_s=60)
    app = create_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent.test") as c:
        yield app, c, s
    await app.state.runs.close_all("fin")
    app.state.store.close()
    shutil.rmtree(s.claude_projects / encode(root / "vivo"), ignore_errors=True)


def kinds(run):
    return [e["kind"] for e in run.events]


async def test_live_nuevo_retomar_permiso(live):
    app, c, s = live
    acc = (await c.post("/api/accounts", json={"alias": os.environ["AIRWORK_LIVE_ACCOUNT"]}, headers=H)).json()["id"]
    r = await c.post("/api/runs", json={"project": "vivo", "account_id": acc, "model": "haiku",
                                        "prompt": "Recuerda la palabra pera. Responde solo: ok"}, headers=H)
    assert r.status_code == 200, r.text
    run = app.state.runs.get(r.json()["run_id"])
    sid = r.json()["session_id"]
    await wait_for(lambda: "result" in kinds(run) or "closed" in kinds(run), timeout=120)
    print([{k: v for k, v in e.items() if k != 'ts'} for e in run.events if e['kind'] in ('error', 'result', 'rate_limit')])
    res = next(e for e in run.events if e["kind"] == "result")
    if res["is_error"] and res.get("api_error_status") == 429:
        err = (await c.get("/api/accounts")).json()[0]["last_error"]
        assert err, "la cuenta sin cuota debería quedar marcada"
        pytest.skip(f"cuenta sin cuota; quedó marcada con: {err}")
    assert not res["is_error"], res
    assert run.session_id == sid
    jsonl = s.claude_projects / encode(s.programming_root / "vivo") / f"{sid}.jsonl"
    assert jsonl.exists()

    # permiso: Bash en modo default debe pedir aprobación; la denegamos
    await c.post(f"/api/runs/{run.id}/messages",
                 json={"prompt": "Ejecuta con la herramienta Bash el comando: touch creado.txt"}, headers=H)
    await wait_for(lambda: "permission_request" in kinds(run) or kinds(run).count("result") == 2, timeout=120)
    req = next((e for e in run.events if e["kind"] == "permission_request"), None)
    assert req is not None, kinds(run)
    assert (await c.post(f"/api/permissions/{req['req_id']}", json={"decision": "deny"}, headers=H)).json()["ok"]
    await wait_for(lambda: kinds(run).count("result") == 2, timeout=120)
    assert not (s.programming_root / "vivo" / "creado.txt").exists()
    await run.close("test")

    # retomar recién escrito: la heurística lo marca como abierto (409); fork funciona y recuerda
    r = await c.post("/api/runs", json={"project": "vivo", "session_id": sid, "prompt": "x"}, headers=H)
    assert r.status_code == 409, r.text
    os.utime(jsonl, (0, 0))
    r = await c.post("/api/runs", json={"project": "vivo", "session_id": sid,
                                        "prompt": "¿Qué palabra te pedí recordar? Responde solo la palabra."},
                     headers=H)
    assert r.status_code == 200, r.text
    run2 = app.state.runs.get(r.json()["run_id"])
    await wait_for(lambda: "result" in kinds(run2) or "closed" in kinds(run2), timeout=120)
    res = next(e for e in run2.events if e["kind"] == "result")
    assert "pera" in (res["result"] or "").lower(), res
    assert run2.session_id == sid

    ctx = (await c.get(f"/api/sessions/{sid}/context")).json()
    print(ctx.get("totalTokens"), ctx.get("maxTokens"))
    assert ctx["totalTokens"] > 0
