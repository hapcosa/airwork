"""Proyectos de ~/programacion y sus directorios de historial en ~/.claude/projects."""
from __future__ import annotations

import re
from pathlib import Path

from .config import Settings

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NotFound(Exception):
    pass


def encode(path: str | Path) -> str:
    """Misma codificación que usa Claude Code para nombrar ~/.claude/projects/<dir>."""
    return "".join(c if c.isalnum() else "-" for c in str(path))


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_project(s: Settings, name: str) -> Path:
    """Devuelve la ruta del proyecto o NotFound. Solo nombres simples dentro de la raíz."""
    if not _NAME_RE.match(name) or name in (".", ".."):
        raise NotFound(name)
    p = s.programming_root / name
    if not p.is_dir() or not is_within(p, s.programming_root):
        raise NotFound(name)
    return p


def session_dirs(s: Settings, project_path: Path) -> list[Path]:
    """Directorio de historial del proyecto más los de sus worktrees (<base>--claude-worktrees-*)."""
    base = encode(project_path)
    if not s.claude_projects.is_dir():
        return []
    out = []
    for d in s.claude_projects.iterdir():
        if d.is_dir() and (d.name == base or d.name.startswith(base + "--")):
            out.append(d)
    return out


def list_projects(s: Settings) -> list[dict]:
    if not s.programming_root.is_dir():
        return []
    rows = []
    for p in sorted(s.programming_root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir() or p.name.startswith(".") or not _NAME_RE.match(p.name):
            continue
        if not is_within(p, s.programming_root):
            continue  # symlink que sale de la raíz
        dirs = session_dirs(s, p)
        files = [f for d in dirs for f in d.glob("*.jsonl")]
        is_git = (p / ".git").exists()
        if not is_git and not files:
            continue
        last = max((f.stat().st_mtime for f in files), default=None)
        rows.append({"name": p.name, "git": is_git, "sessions": len(files), "last_activity": last})
    return rows


def allowed_cwd(s: Settings, cwd: str | Path) -> bool:
    """Un run solo puede ejecutarse dentro de ~/programacion (incluye .claude/worktrees)."""
    p = Path(cwd)
    return p.is_absolute() and p.is_dir() and is_within(p, s.programming_root)
