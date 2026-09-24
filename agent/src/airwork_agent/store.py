"""SQLite del agente: cuentas (perfiles CLAUDE_CONFIG_DIR) y preferencias por proyecto o sesión."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

from .config import Settings
from .projects import is_within

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"^sk-ant-oat01-[A-Za-z0-9_-]{20,}$")
MODELS = ("fable", "opus", "sonnet", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    alias TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    use_headroom INTEGER NOT NULL DEFAULT 0,
    token_enc BLOB,
    created_at REAL NOT NULL,
    last_error TEXT,
    last_error_at REAL
);
CREATE TABLE IF NOT EXISTS prefs (
    scope TEXT NOT NULL CHECK (scope IN ('project', 'session')),
    key TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    model TEXT,
    effort TEXT,
    permission_mode TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


class InvalidInput(ValueError):
    pass


def _private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        os.chmod(path, 0o600)


def load_or_create_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError(f"{path} tiene permisos demasiado abiertos; usa chmod 600")
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class Store:
    def __init__(self, s: Settings):
        self.s = s
        _private_file(s.db_path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(s.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        os.chmod(s.db_path, 0o600)
        self._fernet: Fernet | None = None

    def close(self) -> None:
        self._db.close()

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(load_or_create_key(self.s.key_path))
        return self._fernet

    # --- cuentas -------------------------------------------------------------

    def profile_dir(self, alias: str) -> Path:
        return self.s.accounts_root / alias

    def check_profile(self, alias: str) -> dict:
        """Estado del perfil sin leer credenciales: existe, tiene login, headroom fijo en settings."""
        d = self.profile_dir(alias)
        st = {"exists": d.is_dir() and is_within(d, self.s.accounts_root), "logged_in": False,
              "settings_base_url": False}
        if not st["exists"]:
            return st
        st["logged_in"] = (d / ".credentials.json").is_file()
        try:
            cfg = json.loads((d / "settings.json").read_text())
            st["settings_base_url"] = bool((cfg.get("env") or {}).get("ANTHROPIC_BASE_URL"))
        except (OSError, ValueError):
            pass
        return st

    def _public(self, row: sqlite3.Row) -> dict:
        st = self.check_profile(row["alias"])
        return {
            "id": row["id"], "alias": row["alias"], "label": row["label"],
            "use_headroom": bool(row["use_headroom"]), "has_token": row["token_enc"] is not None,
            "profile": st, "last_error": row["last_error"], "last_error_at": row["last_error_at"],
        }

    def list_accounts(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM accounts ORDER BY alias").fetchall()
        return [self._public(r) for r in rows]

    def get_account(self, account_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._public(row) if row else None

    def _validate_token(self, token: str | None) -> bytes | None:
        if token is None:
            return None
        if not self.s.allow_tokens:
            raise InvalidInput("El modo token está desactivado (AIRWORK_ALLOW_TOKENS=0)")
        if not TOKEN_RE.match(token):
            raise InvalidInput("El token debe tener el formato sk-ant-oat01-…")
        return self.fernet.encrypt(token.encode())

    def create_account(self, alias: str, label: str = "", use_headroom: bool = False,
                       token: str | None = None) -> dict:
        if not ALIAS_RE.match(alias):
            raise InvalidInput("alias: minúsculas, números, - o _ (máx. 32)")
        st = self.check_profile(alias)
        if not st["exists"]:
            raise InvalidInput(f"No existe el perfil {self.profile_dir(alias)}; créalo con rc-profile {alias}")
        if st["settings_base_url"]:
            raise InvalidInput("El settings.json del perfil fija ANTHROPIC_BASE_URL; quítalo (rc-profile lo hace)")
        enc = self._validate_token(token)
        with self._lock, self._db:
            try:
                cur = self._db.execute(
                    "INSERT INTO accounts (alias, label, use_headroom, token_enc, created_at) VALUES (?, ?, ?, ?, ?)",
                    (alias, label, int(use_headroom), enc, time.time()),
                )
            except sqlite3.IntegrityError:
                raise InvalidInput(f"Ya existe la cuenta {alias}") from None
        return self.get_account(cur.lastrowid)  # type: ignore[return-value]

    def update_account(self, account_id: int, *, label: str | None = None, use_headroom: bool | None = None,
                       token: str | None = None, clear_token: bool = False) -> dict | None:
        sets, args = [], []
        if label is not None:
            sets.append("label = ?"); args.append(label)
        if use_headroom is not None:
            sets.append("use_headroom = ?"); args.append(int(use_headroom))
        if clear_token:
            sets.append("token_enc = NULL")
        elif token is not None:
            sets.append("token_enc = ?"); args.append(self._validate_token(token))
        if sets:
            with self._lock, self._db:
                self._db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", (*args, account_id))
        return self.get_account(account_id)

    def delete_account(self, account_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM accounts WHERE id = ?", (account_id,)).rowcount > 0

    def record_error(self, account_id: int, error: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE accounts SET last_error = ?, last_error_at = ? WHERE id = ?",
                             (error, time.time() if error else None, account_id))

    def child_env(self, account_id: int) -> dict[str, str]:
        """Entorno del proceso claude para esa cuenta. Es lo único que ve el token descifrado."""
        with self._lock:
            row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise InvalidInput("Cuenta inexistente")
        env = {"CLAUDE_CONFIG_DIR": str(self.profile_dir(row["alias"]))}
        if row["token_enc"] is not None:
            if not self.s.allow_tokens:
                raise InvalidInput("La cuenta tiene token pero el modo token está desactivado")
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.fernet.decrypt(row["token_enc"]).decode()
        if row["use_headroom"]:
            env["ANTHROPIC_BASE_URL"] = self.s.headroom_url
        return env

    # --- preferencias ---------------------------------------------------------

    def get_prefs(self, scope: str, key: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM prefs WHERE scope = ? AND key = ?", (scope, key)).fetchone()
        if not row:
            return {"account_id": None, "model": None, "effort": None, "permission_mode": None}
        return {k: row[k] for k in ("account_id", "model", "effort", "permission_mode")}

    def set_prefs(self, scope: str, key: str, *, account_id: int | None = None, model: str | None = None,
                  effort: str | None = None, permission_mode: str | None = None) -> dict:
        validate_run_options(model, effort, permission_mode)
        if scope not in ("project", "session"):
            raise InvalidInput("scope inválido")
        if account_id is not None and self.get_account(account_id) is None:
            raise InvalidInput("Cuenta inexistente")
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO prefs (scope, key, account_id, model, effort, permission_mode, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (scope, key) DO UPDATE SET account_id = excluded.account_id,
                     model = excluded.model, effort = excluded.effort,
                     permission_mode = excluded.permission_mode, updated_at = excluded.updated_at""",
                (scope, key, account_id, model, effort, permission_mode, time.time()),
            )
        return self.get_prefs(scope, key)

    def effective_prefs(self, project: str, session_id: str | None) -> dict:
        """La preferencia de la sesión gana campo a campo a la del proyecto."""
        proj = self.get_prefs("project", project)
        if not session_id:
            return proj
        sess = self.get_prefs("session", session_id)
        return {k: sess[k] if sess[k] is not None else proj[k] for k in proj}


_MODEL_RE = re.compile(r"^claude-[a-z0-9.-]{1,60}$")


def validate_run_options(model: str | None, effort: str | None, permission_mode: str | None) -> None:
    if model is not None and model not in MODELS and not _MODEL_RE.match(model):
        raise InvalidInput(f"modelo inválido: {model}")
    if effort is not None and effort not in EFFORTS:
        raise InvalidInput(f"effort inválido: {effort}")
    if permission_mode is not None and permission_mode not in PERMISSION_MODES:
        raise InvalidInput(f"permission_mode no permitido: {permission_mode}")
