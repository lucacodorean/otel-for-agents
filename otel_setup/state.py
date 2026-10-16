"""Small JSON state files under the data directory."""
from __future__ import annotations

import json
import os
from pathlib import Path


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save_json(path: Path, data, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
