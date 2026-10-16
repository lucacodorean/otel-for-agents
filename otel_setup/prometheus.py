"""Helpers for the development Prometheus in infra/prometheus."""
from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

from otel_setup.config import Config
from otel_setup.errors import SetupError
from otel_setup.render import PROM_DIR, render_prometheus_files


def hash_password(password: str) -> str:
    try:
        import bcrypt
    except ImportError as exc:  # pragma: no cover
        raise SetupError("the 'bcrypt' package is required (pip install bcrypt)") from exc
    raw = password.encode()
    if not raw or len(raw) > 72:
        raise SetupError("password must be 1-72 bytes (bcrypt limit)")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=10)).decode()


def prompt_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat: "):
        raise SetupError("passwords do not match")
    return first


def existing_hash(cfg: Config) -> str | None:
    """Reuse the rendered bcrypt hash if it still matches, so re-rendering is idempotent."""
    path = PROM_DIR / "web-config.yml"
    if not path.is_file():
        return None
    m = re.search(rf'^\s+{re.escape(cfg.username)}:\s*"(\$2[aby]\$[^"]+)"', path.read_text(), re.M)
    if not m:
        return None
    try:
        import bcrypt
        return m.group(1) if bcrypt.checkpw(cfg.password.encode(), m.group(1).encode()) else None
    except (ImportError, ValueError):
        return None


def render_config(cfg: Config, out) -> None:
    files = render_prometheus_files(cfg, existing_hash(cfg) or hash_password(cfg.password))
    for name, content in files.items():
        mode = 0o644  # read by the non-root user inside the container
        out.write(Path(PROM_DIR) / name, content, mode)
    if not out.dry_run:
        os.chmod(PROM_DIR / "scrape-password", 0o644)
