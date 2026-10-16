"""Rendering of the Collector configuration.

The Collector itself runs as a Docker container (infra/docker); this module only
writes its configuration into the shared data volume. Starting, recreating and
removing the container is done by infra/docker/otel_setup.sh, because a container
cannot (and should not) control the Docker daemon.
"""
from __future__ import annotations

import os
from pathlib import Path

from otel_setup import paths
from otel_setup.config import Config
from otel_setup.render import render_collector_config
from otel_setup.state import load_json


class Out:
    """Prints actions; in dry-run mode nothing is written."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def say(self, msg: str) -> None:
        print(("[dry-run] would " if self.dry_run else "") + msg)

    def write(self, path: Path, content: str, mode: int = 0o644) -> None:
        self.say(f"write {path}")
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)


def load_allowlist() -> dict[str, dict[str, dict[str, list[str]]]]:
    """Allowlist saved by `marketplace sync`. Older files stored plain plugin-name lists."""
    raw = load_json(paths.allowlist_path(), {})
    return {mp: ({p: {"skills": [], "agents": []} for p in plugins} if isinstance(plugins, list) else plugins)
            for mp, plugins in raw.items()}


def render_files(cfg: Config, out: Out) -> None:
    """Render the Collector config from .env and the synced allowlist."""
    allowlist = load_allowlist()
    missing = [m.name for m in cfg.marketplaces if m.name not in allowlist]
    if missing:
        print(f"warning: marketplace(s) not synced yet: {', '.join(missing)}. "
              "Run `marketplace sync`; until then no data point matches them.")
    out.write(paths.rendered_config_path(), render_collector_config(cfg, allowlist))
