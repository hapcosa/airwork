"""Configuración del agente, leída de variables de entorno con defaults seguros."""
from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

# Variables que el agente nunca hereda al proceso hijo: cada run decide las suyas.
CHILD_ENV_BLOCKLIST = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE")


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


@dataclass
class Settings:
    home: Path = field(default_factory=Path.home)
    programming_root: Path = None  # type: ignore[assignment]
    claude_projects: Path = None  # type: ignore[assignment]
    accounts_root: Path = None  # type: ignore[assignment]
    data_dir: Path = None  # type: ignore[assignment]
    state_dir: Path = None  # type: ignore[assignment]
    config_dir: Path = None  # type: ignore[assignment]
    host: str = "127.0.0.1"
    port: int = 8790
    pc_name: str = field(default_factory=socket.gethostname)
    access_team_domain: str = ""  # ej. mi-team.cloudflareaccess.com
    access_aud: str = ""
    allowed_emails: tuple[str, ...] = ()
    dev: bool = False
    allow_tokens: bool = False
    claude_bin: str = ""
    headroom_url: str = "http://127.0.0.1:8787"
    idle_timeout_s: int = 15 * 60
    permission_timeout_s: int = 10 * 60
    max_runs: int = 4

    def __post_init__(self) -> None:
        h = self.home
        self.programming_root = self.programming_root or h / "programacion"
        self.claude_projects = self.claude_projects or h / ".claude" / "projects"
        self.accounts_root = self.accounts_root or h / ".claude-accounts"
        self.data_dir = self.data_dir or h / ".local" / "share" / "airwork"
        self.state_dir = self.state_dir or h / ".local" / "state" / "airwork"
        self.config_dir = self.config_dir or h / ".config" / "airwork"
        self.claude_bin = self.claude_bin or shutil.which("claude") or "claude"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def key_path(self) -> Path:
        return self.config_dir / "key"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.log"

    @classmethod
    def from_env(cls) -> "Settings":
        h = Path.home()
        return cls(
            home=h,
            programming_root=_path("AIRWORK_PROGRAMMING_ROOT", h / "programacion"),
            claude_projects=_path("AIRWORK_CLAUDE_PROJECTS", h / ".claude" / "projects"),
            accounts_root=_path("AIRWORK_ACCOUNTS_ROOT", h / ".claude-accounts"),
            data_dir=_path("AIRWORK_DATA_DIR", h / ".local" / "share" / "airwork"),
            state_dir=_path("AIRWORK_STATE_DIR", h / ".local" / "state" / "airwork"),
            config_dir=_path("AIRWORK_CONFIG_DIR", h / ".config" / "airwork"),
            host=os.environ.get("AIRWORK_HOST", "127.0.0.1"),
            port=int(os.environ.get("AIRWORK_PORT", "8790")),
            pc_name=os.environ.get("AIRWORK_PC_NAME") or socket.gethostname(),
            access_team_domain=os.environ.get("AIRWORK_ACCESS_TEAM_DOMAIN", ""),
            access_aud=os.environ.get("AIRWORK_ACCESS_AUD", ""),
            allowed_emails=tuple(
                e.strip().lower() for e in os.environ.get("AIRWORK_ALLOWED_EMAILS", "").split(",") if e.strip()
            ),
            dev=_bool("AIRWORK_DEV"),
            allow_tokens=_bool("AIRWORK_ALLOW_TOKENS"),
            claude_bin=os.environ.get("AIRWORK_CLAUDE_BIN", ""),
            headroom_url=os.environ.get("AIRWORK_HEADROOM_URL", "http://127.0.0.1:8787"),
            idle_timeout_s=int(os.environ.get("AIRWORK_IDLE_TIMEOUT_S", str(15 * 60))),
            permission_timeout_s=int(os.environ.get("AIRWORK_PERMISSION_TIMEOUT_S", str(10 * 60))),
            max_runs=int(os.environ.get("AIRWORK_MAX_RUNS", "4")),
        )

    def validate_for_serving(self) -> None:
        """Se niega a arrancar si la configuración deja la API abierta."""
        if self.host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"AIRWORK_HOST={self.host}: el agente solo escucha en loopback")
        if not self.dev and not (self.access_team_domain and self.access_aud and self.allowed_emails):
            raise RuntimeError(
                "Faltan AIRWORK_ACCESS_TEAM_DOMAIN, AIRWORK_ACCESS_AUD o AIRWORK_ALLOWED_EMAILS "
                "(o AIRWORK_DEV=1 para desarrollo local)"
            )


def scrub_process_env() -> None:
    """El SDK hereda os.environ en el hijo; se limpian aquí las variables sensibles."""
    for k in CHILD_ENV_BLOCKLIST:
        os.environ.pop(k, None)
