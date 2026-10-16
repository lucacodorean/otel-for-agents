"""Render the Collector config, its environment file and the dev Prometheus files."""
from __future__ import annotations

import json
import re
from pathlib import Path

from otel_setup.config import Config
from otel_setup.paths import REPO_ROOT

COLLECTOR_TEMPLATE = REPO_ROOT / "infra" / "collector" / "config.yaml.example"
PROM_DIR = REPO_ROOT / "infra" / "prometheus"
BASE_PROCESSORS = ["memory_limiter", "resource_detection", "attributes/privacy"]


def _regex(names: list[str]) -> str:
    return "^(" + "|".join(re.escape(n) for n in sorted(names)) + ")$"


def _q(text: str) -> str:
    """Double-quoted scalar valid in both OTTL and YAML."""
    return json.dumps(text)


def marketplace_statements(cfg: Config, allowlist: dict, agent_attrs: tuple[str, ...] = ("agent.name",)) -> list[str]:
    """OTTL statements that label a record with its marketplace (and owning plugin).

    Real plugin installs carry plugin.name; components copied into a project (e.g. by
    APM) arrive as plain skill.name / agent name, so those are matched too. `agent_attrs`
    are the attribute names that hold an agent name: `agent.name` on metrics, and also
    `agent_type` on the subagent_completed event.
    """
    statements = []
    for mp in cfg.marketplaces:
        for plugin, comps in sorted(allowlist.get(mp.name, {}).items()):
            by_plugin = f'(attributes["plugin.name"] != nil and attributes["plugin.name"] == {_q(plugin)})'
            by_component = []
            if comps.get("skills"):
                by_component.append(
                    f'(attributes["skill.name"] != nil and IsMatch(attributes["skill.name"], {_q(_regex(comps["skills"]))}))')
            if comps.get("agents"):
                for attr in agent_attrs:
                    by_component.append(
                        f'(attributes["{attr}"] != nil and IsMatch(attributes["{attr}"], {_q(_regex(comps["agents"]))}))')
            matches = " or ".join([by_plugin, *by_component])
            statements.append(
                f'set(attributes["marketplace"], {_q(mp.name)}) where attributes["marketplace"] == nil and ({matches})')
            if by_component:
                # Attribute the owning plugin when Claude Code did not report one.
                statements.append(
                    f'set(attributes["plugin.name"], {_q(plugin)}) where attributes["marketplace"] == {_q(mp.name)} '
                    f'and attributes["plugin.name"] == nil and ({" or ".join(by_component)})')
    return statements


def unmatched_condition(cfg: Config) -> str:
    """True for a record that matched no marketplace and is not an extra-allowlisted plugin.

    The nil guard matters: with error_mode ignore, an IsMatch on a missing attribute
    would error and the record would be kept.
    """
    cond = 'attributes["marketplace"] == nil'
    if cfg.extra_plugins:
        cond += (
            ' and (attributes["plugin.name"] == nil or not IsMatch(attributes["plugin.name"], '
            f"{_q(_regex(list(cfg.extra_plugins)))}))"
        )
    return cond


def _transform_block(name: str, key: str, context: str, statements: list[str]) -> list[str]:
    lines = [f"  {name}:", "    error_mode: ignore"]
    if statements:
        lines += [f"    {key}:", f"      - context: {context}", "        statements:"]
        lines += [f"          - {_q(s)}" for s in statements]
    else:
        lines += [f"    {key}: []"]
    return lines


def marketplace_processors(cfg: Config, allowlist: dict) -> tuple[str, list[str]]:
    """Return (YAML block, processor ids) for the metrics transform and filter processors.

    No marketplace configured -> nothing is added: everything passes through.
    """
    if not cfg.marketplaces:
        return "", []

    ids = ["transform/marketplace"]
    lines = _transform_block("transform/marketplace", "metric_statements", "datapoint",
                             marketplace_statements(cfg, allowlist))
    if cfg.filter_mode == "drop":
        lines += [
            "  filter/marketplace:", "    error_mode: ignore", "    metrics:", "      datapoint:",
            f"        - {_q(unmatched_condition(cfg))}",
        ]
        ids.append("filter/marketplace")
    return "\n".join(lines) + "\n", ids


def exporter_tls(cfg: Config) -> str:
    if not cfg.tls_enabled:
        return ""
    lines = ["      tls:", f"        insecure_skip_verify: {str(cfg.insecure_skip_verify).lower()}"]
    if cfg.container_ca_file:
        lines.append(f"        ca_file: {_q(cfg.container_ca_file)}")
    return "\n".join(lines)


def render_collector_config(cfg: Config, allowlist: dict, template: str | None = None) -> str:
    text = template if template is not None else COLLECTOR_TEMPLATE.read_text()
    block, ids = marketplace_processors(cfg, allowlist)
    processors = BASE_PROCESSORS + ids + ["batch"]
    connectors, event_pipelines = "", ""
    if cfg.quality_signals:
        from otel_setup import events  # deferred: events imports helpers from this module
        ev_block, ev_ids = events.processors_block(cfg, allowlist)
        block += ev_block
        connectors, event_pipelines = events.connectors_block(), events.pipelines_block(ev_ids)
    out = (
        text.replace("@@MARKETPLACE_PROCESSORS@@\n", block)
        .replace("@@CONNECTORS@@\n", connectors)
        .replace("@@EVENT_PIPELINES@@\n", event_pipelines)
        .replace("@@EXPORTER_TLS@@\n", exporter_tls(cfg) + "\n" if cfg.tls_enabled else "")
        .replace("@@WAL_DIR@@", _q(str(cfg.wal_dir)))
        .replace("@@METRICS_PROCESSORS@@", ", ".join(processors))
    )
    leftover = re.findall(r"@@\w+@@", out)
    if leftover:
        raise ValueError(f"unrendered markers in collector config: {leftover}")
    return out


def render_prometheus_files(cfg: Config, bcrypt_hash: str) -> dict[str, str]:
    web = (PROM_DIR / "web-config.yml.example").read_text()
    web = web.replace("__USERNAME__", cfg.username).replace("__BCRYPT_HASH__", bcrypt_hash)
    prom = (PROM_DIR / "prometheus.yml.example").read_text()
    prom = prom.replace("__SCHEME__", "http").replace("__USERNAME__", cfg.username)
    return {"web-config.yml": web, "prometheus.yml": prom, "scrape-password": cfg.password}
