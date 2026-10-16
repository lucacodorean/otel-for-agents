"""Command line interface: `otel_setup <command>`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from otel_setup import __version__, marketplace, paths, prometheus, service, settings, status
from otel_setup.config import Config, load_config
from otel_setup.errors import SetupError
from otel_setup.state import load_json, save_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="otel_setup", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--env-file", type=Path, help="path to the .env file (default: <repo>/.env)")
    sub = p.add_subparsers(dest="command", required=True)

    def mutating(name: str, help: str, parent=sub) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help)
        sp.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
        return sp

    mutating("enable", "merge the Claude Code OTEL settings into ~/.claude/settings.json")
    mutating("disable", "remove only the settings this tool added")
    sub.add_parser("status", help="show settings, service, health and endpoint state")

    mp = sub.add_parser("marketplace", help="marketplace catalogs").add_subparsers(dest="action", required=True)
    mutating("sync", "fetch catalogs, derive the allowlist and re-render the Collector config", mp)
    mp.add_parser("list", help="show configured marketplaces and their plugins")

    col = sub.add_parser("collector", help="local Collector service").add_subparsers(dest="action", required=True)
    mutating("render", "render the Collector config from .env and the synced allowlist "
                       "(start/restart/stop the container with infra/docker/otel_setup.sh)", col)

    pr = sub.add_parser("prometheus", help="development Prometheus helpers").add_subparsers(dest="action", required=True)
    hp = pr.add_parser("hash-password", help="print a bcrypt hash for the Prometheus web config")
    hp.add_argument("--from-env", action="store_true", help="use PROMETHEUS_PASSWORD from .env")
    mutating("render-config", "render web-config.yml, prometheus.yml and scrape-password from .env", pr)
    return p


# --- commands ----------------------------------------------------------------

def cmd_enable(cfg: Config, out: service.Out) -> None:
    path = paths.claude_settings_path()
    state = load_json(paths.state_path(), {}).get("claude_settings", {})
    plan = settings.merge(settings.read_settings(path), settings.managed_env(cfg.export_interval_ms, cfg.quality_signals), state, path)
    for w in plan.warnings:
        print(f"warning: {w}")
    if not plan.modified:
        print(f"{path}: already up to date")
        return
    for k in plan.added:
        print(f"  + {k}")
    for k in plan.changed:
        print(f"  ~ {k} (previous value saved)")
    if out.dry_run:
        out.say(f"back up and update {path}")
        return
    bak = settings.backup(path)
    settings.write_settings(path, plan.document)
    full = load_json(paths.state_path(), {})
    full["claude_settings"] = plan.state
    save_json(paths.state_path(), full)
    print(f"updated {path}" + (f" (backup: {bak})" if bak else ""))


def cmd_disable(cfg: Config | None, out: service.Out) -> None:
    path = paths.claude_settings_path()
    full = load_json(paths.state_path(), {})
    state = full.get("claude_settings")
    if not state:
        print("nothing to disable: this tool has not enabled telemetry")
        return
    plan = settings.unmerge(settings.read_settings(path), state, path)
    for k in plan.kept:
        print(f"  = {k} left alone (changed since enable)")
    if not plan.modified:
        print(f"{path}: no managed keys to remove")
        return
    for k in plan.removed:
        print(f"  - {k}")
    for k in plan.restored:
        print(f"  ~ {k} (previous value restored)")
    if out.dry_run:
        out.say(f"back up and update {path}")
        return
    bak = settings.backup(path)
    settings.write_settings(path, plan.document)
    full.pop("claude_settings", None)
    save_json(paths.state_path(), full)
    print(f"updated {path}" + (f" (backup: {bak})" if bak else ""))


def cmd_marketplace_sync(cfg: Config, out: service.Out) -> None:
    if not cfg.marketplaces:
        print("no marketplace configured (MARKETPLACES is empty): nothing to sync, everything passes through")
        return
    allowlist = marketplace.sync(cfg, paths.cache_dir() / "marketplaces")
    for name, plugins in allowlist.items():
        skills = sum(len(c["skills"]) for c in plugins.values())
        agents = sum(len(c["agents"]) for c in plugins.values())
        print(f"  {name}: {len(plugins)} plugin(s), {skills} skill(s), {agents} agent(s)")
    out.say(f"write {paths.allowlist_path()}")
    if not out.dry_run:
        save_json(paths.allowlist_path(), allowlist, 0o644)
    service.render_files(cfg, out)


def cmd_marketplace_list(cfg: Config, out=None) -> None:
    if not cfg.marketplaces:
        print("no marketplace configured")
    allowlist = service.load_allowlist()
    for mp in cfg.marketplaces:
        print(f"{mp.name}  {mp.source}{'@' + mp.ref if mp.ref else ''}")
        plugins = allowlist.get(mp.name)
        if plugins is None:
            print("  (not synced; run `otel_setup marketplace sync`)")
        for plugin, comps in (plugins or {}).items():
            print(f"  - {plugin}")
            for kind in ("skills", "agents"):
                if comps[kind]:
                    print(f"      {kind}: {', '.join(comps[kind])}")
    if cfg.extra_plugins:
        print("extra allowlist: " + ", ".join(cfg.extra_plugins))


def cmd_hash_password(args, cfg_loader) -> None:
    password = cfg_loader().password if args.from_env else prometheus.prompt_password()
    print(prometheus.hash_password(password))


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    load = lambda: load_config(args.env_file)  # noqa: E731
    out = service.Out(getattr(args, "dry_run", False))
    cmd, action = args.command, getattr(args, "action", None)

    if cmd == "prometheus" and action == "hash-password":
        cmd_hash_password(args, load)
    elif cmd == "disable":
        cmd_disable(None, out)
    else:
        cfg = load()
        if cmd == "enable":
            cmd_enable(cfg, out)
        elif cmd == "status":
            return 0 if status.report(cfg) else 1
        elif cmd == "marketplace" and action == "sync":
            cmd_marketplace_sync(cfg, out)
        elif cmd == "marketplace" and action == "list":
            cmd_marketplace_list(cfg)
        elif cmd == "collector" and action == "render":
            service.render_files(cfg, out)
        elif cmd == "prometheus" and action == "render-config":
            prometheus.render_config(cfg, out)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
