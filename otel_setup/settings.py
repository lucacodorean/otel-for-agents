"""Merge/unmerge the Claude Code OTEL settings in ~/.claude/settings.json.

Claude Code ignores OTEL variables in a repository's .claude/settings*.json, so
the user-level file is the only place this tool writes. The file is never
overwritten wholesale: only the managed keys of its `env` block are touched, a
timestamped backup is taken before every write, and the previous value of any
key we overwrite is recorded so `disable` can restore it.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from otel_setup.errors import SetupError

OTLP_ENDPOINT = "http://127.0.0.1:4318"
# Variables that must stay unset: they would log prompt or response content.
FORBIDDEN_UNSET = ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES")


def managed_env(export_interval_ms: int, quality_signals: bool = False) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": OTLP_ENDPOINT,
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
        "OTEL_LOG_TOOL_DETAILS": "1",
        "OTEL_METRIC_EXPORT_INTERVAL": str(export_interval_ms),
    }
    if quality_signals:
        # Events (skill activations, tool results, ...) go to the same local Collector, which
        # turns them into counters and never stores them. Needs OTEL_LOG_TOOL_DETAILS=1 for
        # real skill and agent names; see the README on what else that flag exposes.
        env["OTEL_LOGS_EXPORTER"] = "otlp"
    return env


@dataclass
class Plan:
    """Result of a merge/unmerge: the new document plus what changed."""
    document: dict
    state: dict
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # modified by the user since enable
    warnings: list[str] = field(default_factory=list)

    @property
    def modified(self) -> bool:
        return bool(self.added or self.changed or self.removed or self.restored)


def read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} is not valid JSON ({exc}); fix it before running this tool") from exc
    if not isinstance(doc, dict):
        raise SetupError(f"{path}: expected a JSON object at the top level")
    return doc


def _env_block(doc: dict, path: Path) -> dict:
    env = doc.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise SetupError(f"{path}: 'env' must be an object")
    return env


def merge(doc: dict, desired: dict[str, str], state: dict, path: Path = Path("settings.json")) -> Plan:
    """Return the settings with `desired` merged into `env`. Pure: `doc` is not mutated."""
    new = json.loads(json.dumps(doc))
    env_existed = "env" in new
    env = _env_block(new, path)
    previous = dict(state.get("previous", {}))  # key -> old value or None (was absent)
    plan = Plan(document=new, state={})
    for key, value in desired.items():
        if key in env:
            if env[key] == value:
                continue
            previous.setdefault(key, env[key])
            plan.changed.append(key)
        else:
            previous.setdefault(key, None)
            plan.added.append(key)
        env[key] = value
    # Keys this tool managed before but no longer wants (e.g. OTEL_LOGS_EXPORTER after
    # QUALITY_SIGNALS=false): remove or restore them, unless the user changed them since.
    written = state.get("written", {})
    for key in state.get("managed", []):
        if key in desired or key not in env or env[key] != written.get(key):
            continue
        old = previous.pop(key, None)
        if old is None:
            del env[key]
            plan.removed.append(key)
        else:
            env[key] = old
            plan.restored.append(key)
    new["env"] = env
    for key in FORBIDDEN_UNSET:
        if key in env:
            plan.warnings.append(f"{key} is set in the env block; prompts/responses may be logged")
    plan.state = {
        "managed": sorted(desired),
        "previous": previous,
        "created_env": state.get("created_env", not env_existed),
        "written": dict(desired),
    }
    if not plan.modified and env_existed:
        plan.document = doc
    return plan


def unmerge(doc: dict, state: dict, path: Path = Path("settings.json")) -> Plan:
    """Remove (or restore) only the keys this tool wrote, if still unchanged."""
    new = json.loads(json.dumps(doc))
    plan = Plan(document=new, state=state)
    env = _env_block(new, path)
    written = state.get("written", {})
    previous = state.get("previous", {})
    for key in state.get("managed", []):
        if key not in env:
            continue
        if env[key] != written.get(key):
            plan.kept.append(key)
            continue
        old = previous.get(key)
        if old is None:
            del env[key]
            plan.removed.append(key)
        else:
            env[key] = old
            plan.restored.append(key)
    if "env" in new:
        if env:
            new["env"] = env
        elif state.get("created_env", False):
            del new["env"]
        else:
            new["env"] = env
    if not plan.modified:
        plan.document = doc
    return plan


def backup(path: Path, now: datetime | None = None) -> Path | None:
    if not path.exists():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"{path.name}.otel-setup-{stamp}.bak")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.otel-setup-{stamp}-{n}.bak")
        n += 1
    shutil.copy2(path, dest)
    return dest


def write_settings(path: Path, doc: dict) -> None:
    """Atomic write that preserves the existing file mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
