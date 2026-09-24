"""Lectura en solo lectura de los .jsonl de Claude Code: resúmenes y eventos para la PWA."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .projects import encode, session_dirs

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TOOL_RESULT_MAX = 4000
TOOL_INPUT_MAX = 2000
_COMMAND_RE = re.compile(r"<command-name>/?([^<]+)</command-name>")
_STDOUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)

_cache_lock = threading.Lock()
_summary_cache: dict[Path, tuple[tuple[float, int], dict]] = {}
_events_cache: dict[Path, tuple[tuple[float, int], list[dict]]] = {}


def _stamp(p: Path) -> tuple[float, int]:
    st = p.stat()
    return (st.st_mtime, st.st_size)


def _lines(p: Path) -> Iterator[dict]:
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict):
                yield o


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_real_prompt(o: dict) -> bool:
    if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain") or o.get("isCompactSummary"):
        return False
    content = (o.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    t = _text_of(content).strip()
    return bool(t) and not t.startswith("<")


def _truncate(s: str, n: int) -> tuple[str, bool]:
    return (s, False) if len(s) <= n else (s[:n], True)


# --- resúmenes ---------------------------------------------------------------

def summarize(p: Path) -> dict:
    stamp = _stamp(p)
    with _cache_lock:
        hit = _summary_cache.get(p)
        if hit and hit[0] == stamp:
            return hit[1]
    info: dict[str, Any] = {
        "id": p.stem, "title": "", "ai_title": "", "first_prompt": "", "last_prompt": "",
        "prompts": 0, "branch": "", "cwd": "", "relocated_cwd": "", "continued_in": "",
        "mtime": stamp[0], "size": stamp[1], "worktree": False, "entrypoint": "",
    }
    custom = ""
    for o in _lines(p):
        t = o.get("type")
        if t == "custom-title":
            custom = o.get("customTitle") or custom
        elif t == "ai-title":
            info["ai_title"] = o.get("aiTitle") or info["ai_title"]
        elif t == "last-prompt":
            info["last_prompt"] = o.get("lastPrompt") or info["last_prompt"]
        elif t == "relocated":
            info["relocated_cwd"] = o.get("relocatedCwd") or info["relocated_cwd"]
        elif t == "continued-in":
            info["continued_in"] = o.get("continuedInSessionId") or info["continued_in"]
        if not info["cwd"] and o.get("cwd"):
            info["cwd"] = o["cwd"]
        if not info["entrypoint"] and o.get("entrypoint"):
            info["entrypoint"] = o["entrypoint"]
        if _is_real_prompt(o):
            info["prompts"] += 1
            info["branch"] = o.get("gitBranch") or info["branch"]
            if not info["first_prompt"]:
                info["first_prompt"] = _text_of(o["message"].get("content")).strip()[:300]
    info["title"] = custom or info["ai_title"] or info["last_prompt"][:120] or info["first_prompt"][:120]
    info["worktree"] = "/.claude/worktrees/" in (info["relocated_cwd"] or info["cwd"])
    with _cache_lock:
        _summary_cache[p] = (stamp, info)
    return info


def list_sessions(s: Settings, project_path: Path, limit: int = 50, before: float | None = None) -> list[dict]:
    files = [f for d in session_dirs(s, project_path) for f in d.glob("*.jsonl") if UUID_RE.match(f.stem)]
    rows = [summarize(f) for f in files]
    rows = [r for r in rows if r["prompts"] > 0 and (before is None or r["mtime"] < before)]
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def find_session(s: Settings, session_id: str) -> Path | None:
    """Busca <id>.jsonl solo en directorios de proyectos dentro de ~/programacion."""
    if not UUID_RE.match(session_id) or not s.claude_projects.is_dir():
        return None
    prefix = encode(s.programming_root) + "-"
    for d in s.claude_projects.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            f = d / f"{session_id}.jsonl"
            if f.is_file():
                return f
    return None


def project_of(s: Settings, session_path: Path) -> str | None:
    """Nombre del proyecto al que pertenece el .jsonl (según su directorio)."""
    prefix = encode(s.programming_root) + "-"
    name = session_path.parent.name
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix):].split("--", 1)[0]
    for p in s.programming_root.iterdir():
        if p.is_dir() and encode(p.name) == rest:
            return p.name
    return None


# --- eventos -------------------------------------------------------------------

def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[imagen]")
        return "\n".join(parts)
    return ""


def _events_from_line(o: dict) -> list[dict]:
    t = o.get("type")
    base = {"ts": o.get("timestamp", ""), "uuid": o.get("uuid", ""), "sidechain": bool(o.get("isSidechain"))}
    out: list[dict] = []
    if t == "user":
        msg = o.get("message") or {}
        content = msg.get("content")
        if o.get("isCompactSummary"):
            text, cut = _truncate(_text_of(content), 20000)
            return [{**base, "kind": "compact_summary", "text": text, "truncated": cut}]
        if o.get("isMeta"):
            return []
        if isinstance(content, str):
            s = content.strip()
            if m := _COMMAND_RE.search(s):
                return [{**base, "kind": "command", "name": m.group(1).strip()}]
            if m := _STDOUT_RE.search(s):
                text, cut = _truncate(m.group(1).strip(), TOOL_RESULT_MAX)
                return [{**base, "kind": "command_output", "text": text, "truncated": cut}] if text else []
            if s and not s.startswith("<"):
                return [{**base, "kind": "prompt", "text": s}]
            return []
        if isinstance(content, list):
            texts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_result":
                    text, cut = _truncate(_tool_result_text(b.get("content")), TOOL_RESULT_MAX)
                    out.append({**base, "kind": "tool_result", "tool_use_id": b.get("tool_use_id", ""),
                                "is_error": bool(b.get("is_error")), "text": text, "truncated": cut})
                elif bt == "text":
                    texts.append(b.get("text", ""))
                elif bt == "image":
                    out.append({**base, "kind": "image"})
            joined = "\n".join(texts).strip()
            if joined and not joined.startswith("<"):
                out.insert(0, {**base, "kind": "prompt", "text": joined})
        return out
    if t == "assistant":
        msg = o.get("message") or {}
        if o.get("isApiErrorMessage"):
            return [{**base, "kind": "error", "text": _text_of(msg.get("content"))}]
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                out.append({**base, "kind": "text", "text": b.get("text", ""), "model": msg.get("model", "")})
            elif bt == "thinking":
                out.append({**base, "kind": "thinking", "text": b.get("thinking", "")})
            elif bt == "tool_use":
                raw = json.dumps(b.get("input", {}), ensure_ascii=False)
                inp, cut = _truncate(raw, TOOL_INPUT_MAX)
                out.append({**base, "kind": "tool_use", "id": b.get("id", ""), "name": b.get("name", ""),
                            "input": inp, "truncated": cut})
        return out
    if t == "system" and o.get("subtype") == "compact_boundary":
        meta = o.get("compactMetadata") or {}
        return [{**base, "kind": "compact", "trigger": meta.get("trigger", ""), "pre_tokens": meta.get("preTokens")}]
    if t == "permission-mode":
        return [{**base, "kind": "meta", "text": f"permisos: {o.get('permissionMode', '')}"}]
    if t == "relocated":
        return [{**base, "kind": "meta", "text": f"cambió a {o.get('relocatedCwd', '')}"}]
    if t == "continued-in":
        return [{**base, "kind": "continued_in", "session_id": o.get("continuedInSessionId", "")}]
    return []


def read_events(p: Path, offset: int | None = None, limit: int = 200, sidechain: bool = False) -> dict:
    """Eventos paginados. offset=None devuelve los últimos `limit` (lo útil en el celular)."""
    stamp = _stamp(p)
    with _cache_lock:
        hit = _events_cache.get(p)
    if hit and hit[0] == stamp:
        events = hit[1]
    else:
        events = [e for o in _lines(p) for e in _events_from_line(o)]
        with _cache_lock:
            _events_cache[p] = (stamp, events)
    if not sidechain:
        events = [e for e in events if not e["sidechain"]]
    total = len(events)
    limit = max(1, min(limit, 1000))
    start = max(0, total - limit) if offset is None else max(0, min(offset, total))
    page = [{**e, "i": start + k} for k, e in enumerate(events[start:start + limit])]
    return {"total": total, "offset": start, "events": page}


def session_cwd(p: Path) -> str:
    """cwd de origen de la sesión: el del primer registro, que coincide con su directorio de historial."""
    return summarize(p)["cwd"]
