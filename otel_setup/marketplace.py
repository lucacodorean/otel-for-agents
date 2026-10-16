"""Fetch marketplace catalogs and derive the plugin allowlist."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from otel_setup.config import Config, Marketplace
from otel_setup.errors import MarketplaceError

CATALOG_PATH = Path(".claude-plugin") / "marketplace.json"


def parse_catalog(text: str, origin: str) -> list[str]:
    """Return the plugin names listed in a marketplace.json document."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MarketplaceError(f"{origin}: invalid JSON ({exc})") from exc
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, list):
        raise MarketplaceError(f"{origin}: expected a 'plugins' array")
    names: list[str] = []
    for i, entry in enumerate(plugins):
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise MarketplaceError(f"{origin}: plugins[{i}] has no 'name'")
        names.append(name.strip())
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise MarketplaceError(f"{origin}: duplicate plugin name(s) in one catalog: {', '.join(dupes)}")
    return names


def parse_entries(text: str, origin: str) -> list[tuple[str, object]]:
    """Return (name, source) for each plugin entry; validates like parse_catalog."""
    names = parse_catalog(text, origin)
    plugins = json.loads(text)["plugins"]
    return [(n, e.get("source")) for n, e in zip(names, plugins)]


def _frontmatter_name(path: Path) -> str | None:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:40]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name" and value.strip():
            return value.strip().strip("'\"")
    return None


def discover_components(plugin_dir: Path) -> dict[str, list[str]]:
    """Skill and agent names a plugin ships: `skills/<dir>/SKILL.md` and `agents/*.md`.

    The name is the frontmatter `name` when present, else the directory/file stem.
    """
    skills = sorted({
        _frontmatter_name(d / "SKILL.md") or d.name
        for d in (plugin_dir / "skills").glob("*") if d.is_dir() and (d / "SKILL.md").is_file()
    })
    agents = sorted({
        _frontmatter_name(f) or f.stem for f in (plugin_dir / "agents").glob("*.md") if f.is_file()
    })
    return {"skills": skills, "agents": agents}


def _git(args: list[str], cwd: Path) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MarketplaceError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def fetch_git(mp: Marketplace, dest: Path) -> None:
    """Shallow-fetch `mp` into `dest` using the user's own git credentials.

    init+fetch (instead of `clone --branch`) so branches, tags and commit SHAs
    all work. Nothing is stored by this tool.
    """
    if shutil.which("git") is None:
        raise MarketplaceError("git is required to sync git marketplaces")
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], dest)
    _git(["remote", "add", "origin", mp.source], dest)
    _git(["fetch", "-q", "--depth", "1", "origin", mp.ref or "HEAD"], dest)
    _git(["checkout", "-q", "FETCH_HEAD"], dest)


def read_marketplace(mp: Marketplace, cache_root: Path) -> dict[str, dict[str, list[str]]]:
    """Return {plugin: {"skills": [...], "agents": [...]}} for one marketplace."""
    if mp.is_local:
        root = Path(mp.source).expanduser()
        if not root.is_dir():
            raise MarketplaceError(f"marketplace {mp.name!r}: local path not found: {root}")
        return _read_catalog(root, mp)
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{mp.name}.", dir=cache_root))
    try:
        fetch_git(mp, tmp)
        names = _read_catalog(tmp, mp)
        final = cache_root / mp.name
        shutil.rmtree(final, ignore_errors=True)
        tmp.rename(final)
        return names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_catalog(root: Path, mp: Marketplace) -> dict[str, dict[str, list[str]]]:
    catalog = root / CATALOG_PATH
    if not catalog.is_file():
        raise MarketplaceError(f"marketplace {mp.name!r}: {CATALOG_PATH} not found in {mp.source}")
    result: dict[str, dict[str, list[str]]] = {}
    for name, source in parse_entries(catalog.read_text(), f"marketplace {mp.name!r}"):
        components = {"skills": [], "agents": []}
        # Only in-repo sources ("./plugins/x") can be inspected; others keep the plugin name only.
        if isinstance(source, str) and source.startswith("./"):
            plugin_dir = (root / source).resolve()
            if plugin_dir.is_dir() and root.resolve() in plugin_dir.parents:
                components = discover_components(plugin_dir)
        result[name] = components
    return result


def build_allowlist(catalogs: dict[str, dict[str, dict[str, list[str]]]]) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Validate {marketplace: {plugin: {skills, agents}}}.

    Plugin names must be unique across marketplaces, and a skill (or agent) name
    must belong to one plugin only, otherwise attribution would be a guess.
    """
    owners: dict[str, dict[str, str]] = {"plugin": {}, "skill": {}, "agent": {}}

    def claim(kind: str, name: str, owner: str) -> None:
        prev = owners[kind].get(name)
        if prev is not None and prev != owner:
            raise MarketplaceError(
                f"{kind} {name!r} is defined by both {prev} and {owner}; {kind} names must be "
                "unique across configured marketplaces")
        owners[kind][name] = owner

    result: dict[str, dict[str, dict[str, list[str]]]] = {}
    for mp_name, plugins in catalogs.items():
        result[mp_name] = {}
        for plugin, comps in plugins.items():
            claim("plugin", plugin, f"marketplace {mp_name!r}")
            for kind, key in (("skill", "skills"), ("agent", "agents")):
                for name in comps.get(key, []):
                    claim(kind, name, f"plugin {plugin!r} ({mp_name})")
            result[mp_name][plugin] = {k: sorted(set(comps.get(k, []))) for k in ("skills", "agents")}
    return result


def sync(cfg: Config, cache_root: Path) -> dict[str, dict[str, dict[str, list[str]]]]:
    catalogs = {mp.name: read_marketplace(mp, cache_root) for mp in cfg.marketplaces}
    return build_allowlist(catalogs)
