import os
import time

import pytest

from airwork_agent import history, projects
from conftest import SID_OLD, SID_WT


def test_list_projects_filtra_sin_git_ni_historial(env):
    rows = projects.list_projects(env)
    assert [r["name"] for r in rows] == ["demo"]
    assert rows[0]["sessions"] == 2 and rows[0]["git"]


@pytest.mark.parametrize("name", ["..", ".", "../etc", "demo/../x", "a b", ""])
def test_resolve_project_rechaza_nombres_raros(env, name):
    with pytest.raises(projects.NotFound):
        projects.resolve_project(env, name)


def test_resolve_project_rechaza_symlink_que_sale(env, tmp_path):
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    os.symlink(fuera, env.programming_root / "escape")
    with pytest.raises(projects.NotFound):
        projects.resolve_project(env, "escape")
    assert "escape" not in [r["name"] for r in projects.list_projects(env)]


def test_list_sessions_incluye_worktrees_y_titulos(env):
    rows = history.list_sessions(env, env.programming_root / "demo")
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {SID_OLD, SID_WT}
    assert by_id[SID_OLD]["title"] == "Revisión del README"
    assert by_id[SID_WT]["title"] == "Título propio"  # custom-title gana a ai-title
    assert by_id[SID_WT]["worktree"] and not by_id[SID_OLD]["worktree"]
    assert by_id[SID_OLD]["prompts"] == 1 and by_id[SID_OLD]["branch"] == "main"


def test_find_session_y_project_of(env):
    p = history.find_session(env, SID_WT)
    assert p is not None and history.project_of(env, p) == "demo"
    assert history.find_session(env, "no-es-uuid") is None
    assert history.find_session(env, "../../etc/passwd") is None


def test_find_session_ignora_proyectos_fuera_de_la_raiz(env):
    sid = "44444444-4444-4444-8444-444444444444"
    d = env.claude_projects / projects.encode("/home/otro/lado")
    d.mkdir()
    (d / f"{sid}.jsonl").write_text("{}\n")
    assert history.find_session(env, sid) is None


def test_read_events_tipos_y_recortes(env):
    p = history.find_session(env, SID_OLD)
    out = history.read_events(p)
    kinds = [e["kind"] for e in out["events"]]
    assert kinds == ["meta", "prompt", "thinking", "text", "tool_use", "tool_result", "command", "compact"]
    tr = out["events"][5]
    assert tr["truncated"] and len(tr["text"]) == history.TOOL_RESULT_MAX
    assert out["events"][6]["name"] == "compact"
    con_sub = history.read_events(p, sidechain=True)
    assert con_sub["total"] == out["total"] + 1


def test_read_events_paginacion_desde_el_final(env):
    p = history.find_session(env, SID_OLD)
    last = history.read_events(p, limit=3)
    assert last["offset"] == last["total"] - 3
    assert [e["i"] for e in last["events"]] == [last["total"] - 3, last["total"] - 2, last["total"] - 1]
    first = history.read_events(p, offset=0, limit=2)
    assert [e["kind"] for e in first["events"]] == ["meta", "prompt"]


def test_cache_se_invalida_al_cambiar_el_archivo(env):
    p = history.find_session(env, SID_OLD)
    assert history.summarize(p)["prompts"] == 1
    with open(p, "a") as fh:
        fh.write('{"type":"user","message":{"role":"user","content":"otro prompt"}}\n')
    t = time.time()
    os.utime(p, (t, t))
    assert history.summarize(p)["prompts"] == 2
