import json
import os
import time

from airwork_agent import history
from conftest import SID_OLD, SID_WT, FakeClient, wait_for

TOKEN = "sk-ant-oat01-" + "B" * 40
JSON = {"Origin": "http://agent.test"}


async def _account(client, alias="cuenta1") -> int:
    r = await client.post("/api/accounts", json={"alias": alias}, headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _events(run):
    return [e["kind"] for e in run.events]


async def test_pc_y_cabeceras(client):
    r = await client.get("/api/pc")
    assert r.status_code == 200
    assert r.json()["email"] == "dev@local"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert (await client.get("/docs")).status_code == 404
    assert (await client.get("/openapi.json")).status_code == 404


async def test_proyectos_y_sesiones(client):
    assert [p["name"] for p in (await client.get("/api/projects")).json()] == ["demo"]
    rows = (await client.get("/api/projects/demo/sessions")).json()
    assert {r["id"] for r in rows} == {SID_OLD, SID_WT}
    assert (await client.get("/api/projects/..%2Fetc/sessions")).status_code == 404
    assert (await client.get("/api/projects/otro/sessions")).json() == []


async def test_leer_sesion(client):
    r = (await client.get(f"/api/sessions/{SID_WT}", params={"offset": 0, "limit": 2})).json()
    assert r["project"] == "demo" and r["summary"]["title"] == "Título propio"
    assert [e["kind"] for e in r["events"]] == ["meta", "prompt"]
    assert (await client.get("/api/sessions/99999999-9999-4999-8999-999999999999")).status_code == 404


async def test_handoffs(client):
    lst = (await client.get("/api/projects/demo/handoffs")).json()
    assert [h["name"] for h in lst] == ["2026-09-24-1200-demo.md"]
    one = (await client.get("/api/projects/demo/handoffs", params={"name": lst[0]["name"]})).json()
    assert one["content"].startswith("# Handoff")
    assert (await client.get("/api/projects/demo/handoffs", params={"name": "../../x.md"})).status_code == 400


async def test_escritura_exige_json_y_origen(client):
    r = await client.post("/api/accounts", json={"alias": "cuenta1"}, headers={"Origin": "https://evil.cl"})
    assert r.status_code == 403
    r = await client.post("/api/accounts", content="alias=cuenta1", headers={**JSON, "Content-Type": "text/plain"})
    assert r.status_code == 415


async def test_run_nuevo_sin_cuenta_da_400(client):
    r = await client.post("/api/runs", json={"project": "demo", "prompt": "hola"}, headers=JSON)
    assert r.status_code == 400 and "cuenta" in r.json()["detail"]


async def test_run_nuevo_flujo_completo(client, app, env):
    acc = await _account(client)
    r = await client.post("/api/runs", json={"project": "demo", "prompt": "hola", "account_id": acc,
                                             "model": "sonnet", "effort": "high"}, headers=JSON)
    assert r.status_code == 200, r.text
    body = r.json()
    run = app.state.runs.get(body["run_id"])
    await wait_for(lambda: "result" in _events(run))
    assert _events(run) == ["prompt", "init", "text", "result"]
    assert run.events[2]["text"] == "eco:hola"
    fc = FakeClient.instances[-1]
    assert fc.options.cwd == str(env.programming_root / "demo")
    assert fc.options.env == {"CLAUDE_CONFIG_DIR": str(env.accounts_root / "cuenta1")}
    assert fc.options.session_id == body["session_id"] and fc.options.resume is None
    assert fc.options.effort == "high" and fc.options.model == "sonnet"
    # la sesión recuerda cuenta y modelo
    prefs = (await client.get(f"/api/prefs/session/{body['session_id']}")).json()
    assert prefs["account_id"] == acc and prefs["model"] == "sonnet"

    # segundo mensaje en el mismo proceso
    r = await client.post(f"/api/runs/{run.id}/messages", json={"prompt": "/compact"}, headers=JSON)
    assert r.status_code == 200
    await wait_for(lambda: _events(run).count("result") == 2)
    assert fc.queries == ["hola", "/compact"]

    r = await client.patch(f"/api/runs/{run.id}", json={"model": "opus", "permission_mode": "plan"}, headers=JSON)
    assert r.json()["model"] == "opus" and fc.model == "opus" and fc.mode == "plan"
    r = await client.patch(f"/api/runs/{run.id}", json={"permission_mode": "bypassPermissions"}, headers=JSON)
    assert r.status_code == 400

    await client.post(f"/api/runs/{run.id}/interrupt", headers=JSON)
    assert fc.interrupted

    # SSE: tras cerrar, el stream entrega todo y termina
    assert (await client.delete(f"/api/runs/{run.id}", headers=JSON)).json() == {"closed": True}
    assert (await client.get(f"/api/runs/{run.id}")).status_code == 404


async def test_sse_formato_y_last_event_id(client, app):
    acc = await _account(client)
    body = (await client.post("/api/runs", json={"project": "demo", "prompt": "hola", "account_id": acc},
                              headers=JSON)).json()
    run = app.state.runs.get(body["run_id"])
    await wait_for(lambda: "result" in _events(run))
    await run.close("test")
    # el run ya no está en el manager: se sirve desde el objeto; lo reinsertamos para leer su buffer
    app.state.runs.runs[run.id] = run
    r = await client.get(f"/api/runs/{run.id}/events", headers={"Last-Event-ID": "2"})
    assert r.headers["content-type"].startswith("text/event-stream")
    blocks = [b for b in r.text.split("\n\n") if b.startswith("id:")]
    ids = [int(b.split("\n")[0][4:]) for b in blocks]
    assert ids == list(range(3, ids[-1] + 1))
    kinds = [b.split("\n")[1][7:] for b in blocks]
    assert kinds[-1] == "closed"
    data = json.loads(blocks[0].split("\n")[2][6:])
    assert data["seq"] == 3


async def test_resume_y_proceso_reutilizado(client, app, env):
    acc = await _account(client)
    r = await client.post("/api/runs", json={"project": "demo", "prompt": "sigue", "session_id": SID_WT,
                                             "account_id": acc}, headers=JSON)
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == SID_WT and not r.json()["reused"]
    fc = FakeClient.instances[-1]
    assert fc.options.resume == SID_WT and not fc.options.fork_session
    assert fc.options.cwd == str(env.programming_root / "demo" / ".claude" / "worktrees" / "feat")
    # la sesión aparece activa en el listado
    rows = (await client.get("/api/projects/demo/sessions")).json()
    assert next(x for x in rows if x["id"] == SID_WT)["active_run"] == r.json()["run_id"]
    # segundo POST a la misma sesión: reutiliza; y la cuenta sale de las prefs de sesión
    r2 = await client.post("/api/runs", json={"project": "demo", "prompt": "otra", "session_id": SID_WT},
                           headers=JSON)
    assert r2.json()["reused"] and r2.json()["run_id"] == r.json()["run_id"]
    assert len(FakeClient.instances) == 1


async def test_resume_detecta_actividad_externa_y_fork(client, app, env):
    acc = await _account(client)
    p = history.find_session(env, SID_OLD)
    now = time.time()
    os.utime(p, (now, now))
    r = await client.post("/api/runs", json={"project": "demo", "prompt": "x", "session_id": SID_OLD,
                                             "account_id": acc}, headers=JSON)
    assert r.status_code == 409 and r.json()["reasons"]
    r = await client.post("/api/runs", json={"project": "demo", "prompt": "x", "session_id": SID_OLD,
                                             "account_id": acc, "fork": True}, headers=JSON)
    assert r.status_code == 200, r.text
    fc = FakeClient.instances[-1]
    assert fc.options.resume == SID_OLD and fc.options.fork_session
    run = app.state.runs.get(r.json()["run_id"])
    await wait_for(lambda: run.session_id == "33333333-3333-4333-8333-333333333333")
    assert app.state.runs.for_session(SID_OLD) is None


async def test_sesion_de_otro_proyecto_da_400(client, env):
    acc = await _account(client)
    (env.programming_root / "otro" / ".git").mkdir()
    r = await client.post("/api/runs", json={"project": "otro", "prompt": "x", "session_id": SID_OLD,
                                             "account_id": acc}, headers=JSON)
    assert r.status_code == 400


async def test_permisos_desde_el_celular(client, app):
    acc = await _account(client)
    body = (await client.post("/api/runs", json={"project": "demo", "prompt": "PERMISO", "account_id": acc},
                              headers=JSON)).json()
    run = app.state.runs.get(body["run_id"])
    await wait_for(lambda: "permission_request" in _events(run))
    req = next(e for e in run.events if e["kind"] == "permission_request")
    assert req["tool"] == "Bash" and json.loads(req["input"]) == {"command": "ls"}
    assert (await client.get(f"/api/runs/{run.id}")).json()["pending_permissions"][0]["req_id"] == req["req_id"]
    r = await client.post(f"/api/permissions/{req['req_id']}", json={"decision": "allow", "scope": "session"},
                          headers=JSON)
    assert r.json() == {"ok": True}
    await wait_for(lambda: "result" in _events(run))
    assert any(e["kind"] == "text" and e["text"] == "permiso:allow" for e in run.events)
    assert (await client.post(f"/api/permissions/{req['req_id']}", json={"decision": "deny"},
                              headers=JSON)).status_code == 404
    # con scope=session, el siguiente uso de Bash ya no pregunta
    await client.post(f"/api/runs/{run.id}/messages", json={"prompt": "PERMISO otra vez"}, headers=JSON)
    await wait_for(lambda: _events(run).count("result") == 2)
    assert _events(run).count("permission_request") == 1


async def test_permiso_denegado_y_timeout(client, app, env):
    env.permission_timeout_s = 0.2
    acc = await _account(client)
    body = (await client.post("/api/runs", json={"project": "demo", "prompt": "PERMISO", "account_id": acc},
                              headers=JSON)).json()
    run = app.state.runs.get(body["run_id"])
    await wait_for(lambda: "result" in _events(run))
    res = next(e for e in run.events if e["kind"] == "permission_resolved")
    assert res["decision"] == "deny"
    assert any(e.get("text") == "permiso:deny" for e in run.events)


async def test_kill_y_maximo_de_runs(client, app, env):
    env.max_runs = 1
    acc = await _account(client)
    r1 = await client.post("/api/runs", json={"project": "demo", "prompt": "a", "account_id": acc}, headers=JSON)
    assert r1.status_code == 200
    r2 = await client.post("/api/runs", json={"project": "demo", "prompt": "b", "account_id": acc}, headers=JSON)
    assert r2.status_code == 429
    assert (await client.post("/api/kill", headers=JSON)).json() == {"closed": 1}
    assert (await client.get("/api/runs")).json() == []


async def test_contexto_de_sesion_existente(client):
    acc = await _account(client)
    r = await client.get(f"/api/sessions/{SID_OLD}/context", params={"account_id": acc})
    assert r.status_code == 200
    assert r.json()["totalTokens"] == 1234 and "gridRows" not in r.json()
    fc = FakeClient.instances[-1]
    assert fc.options.resume == SID_OLD and not fc.connected


async def test_cuentas_nunca_exponen_token(client, env, app):
    env.allow_tokens = True
    r = await client.post("/api/accounts", json={"alias": "cuenta1", "token": TOKEN}, headers=JSON)
    assert r.status_code == 201 and r.json()["has_token"]
    acc = r.json()["id"]
    for resp in (r, await client.get("/api/accounts"),
                 await client.patch(f"/api/accounts/{acc}", json={"label": "x"}, headers=JSON)):
        assert TOKEN not in resp.text and "sk-ant" not in resp.text
    body = (await client.post("/api/runs", json={"project": "demo", "prompt": "hola", "account_id": acc},
                              headers=JSON)).json()
    assert TOKEN not in json.dumps(body)
    assert FakeClient.instances[-1].options.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    # no se puede borrar una cuenta con runs activos
    assert (await client.delete(f"/api/accounts/{acc}", headers=JSON)).status_code == 409
    await app.state.runs.close_all()
    assert (await client.delete(f"/api/accounts/{acc}", headers=JSON)).json() == {"deleted": True}
    assert TOKEN not in env.audit_path.read_text()


async def test_error_de_cuota_queda_registrado(client, app):
    acc = await _account(client)
    body = (await client.post("/api/runs", json={"project": "demo", "prompt": "hola", "account_id": acc},
                              headers=JSON)).json()
    run = app.state.runs.get(body["run_id"])
    await wait_for(lambda: "result" in _events(run))
    from claude_agent_sdk import ResultMessage
    await run._handle(ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                                    is_error=True, num_turns=1, session_id=run.session_id,
                                    result="Claude AI usage limit reached|1790000000"))
    accs = (await client.get("/api/accounts")).json()
    assert "usage limit" in accs[0]["last_error"]
    # texto real observado con Claude Code 2.1.281: solo el status 429 lo delata
    await run._handle(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
                                    num_turns=1, session_id=run.session_id, api_error_status=429,
                                    result="You've hit your limit · resets 3:20pm (America/Santiago)"))
    assert "hit your limit" in (await client.get("/api/accounts")).json()[0]["last_error"]
    # un resultado correcto limpia el error
    await run._handle(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id=run.session_id, result="ok"))
    assert (await client.get("/api/accounts")).json()[0]["last_error"] is None


async def test_prefs_validan_clave(client):
    r = await client.put("/api/prefs/session/no-uuid", json={}, headers=JSON)
    assert r.status_code == 400
    r = await client.put("/api/prefs/project/noexiste", json={}, headers=JSON)
    assert r.status_code == 404
    r = await client.put("/api/prefs/project/demo", json={"model": "sonnet"}, headers=JSON)
    assert r.status_code == 200 and r.json()["model"] == "sonnet"
