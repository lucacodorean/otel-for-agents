"""Filesystem locations used by the tool."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ.get("OTEL_SETUP_HOME", "~/.local/share/otel-setup")).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get("OTEL_SETUP_CACHE", "~/.cache/otel-setup")).expanduser()


def rendered_config_path() -> Path:
    return data_dir() / "config" / "config.yaml"


def state_path() -> Path:
    return data_dir() / "state.json"


def allowlist_path() -> Path:
    return data_dir() / "allowlist.json"


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_SETTINGS_PATH", "~/.claude/settings.json")).expanduser()
