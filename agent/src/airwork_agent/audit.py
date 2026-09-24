"""Log de auditoría (JSON por línea, con rotación) y filtro que enmascara tokens en todo log."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    return TOKEN_RE.sub("sk-ant-***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


def install_redaction() -> None:
    f = RedactFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "airwork"):
        logging.getLogger(name).addFilter(f)


class Audit:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._log = logging.getLogger(f"airwork.audit.{path}")
        self._log.propagate = False
        self._log.setLevel(logging.INFO)
        if not self._log.handlers:
            h = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            h.addFilter(RedactFilter())
            self._log.addHandler(h)
        if path.exists():
            os.chmod(path, 0o600)

    def record(self, email: str, ip: str, action: str, result: str = "ok", **fields) -> None:
        entry = {"ts": time.time(), "email": email, "ip": ip, "action": action, "result": result, **fields}
        self._log.info(json.dumps(entry, ensure_ascii=False, default=str))
