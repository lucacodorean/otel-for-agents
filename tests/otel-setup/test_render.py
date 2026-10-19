"""Rendering of the Collector config (otel_setup.render and otel_setup.events)."""
from __future__ import annotations

import re

import pytest
import yaml

from otel_setup import events, render

ALLOWLIST = {"acme": {
    "p1": {"skills": ["s1", "s.two"], "agents": ["reviewer"]},
    "p2": {"skills": [], "agents": []},
}}

# Labels that may reach Prometheus from events. Anything else (tool_parameters, tool_input,
# error messages, file paths, prompts) must stay in memory: see the README, Privacy.
SAFE_EVENT_LABELS = {
    "skill.name", "plugin.name", "marketplace", "invocation_trigger", "skill.source", "agent_type", "is_async",
    "model", "tool_name", "success", "error_type", "agent.name", "status_code", "hook_event", "hook_name",
}


def rendered(cfg, allowlist=None) -> dict:
    text = render.render_collector_config(cfg, ALLOWLIST if allowlist is None else allowlist)
    assert not re.search(r"@@\w+@@", text)
    return yaml.safe_load(text)


CONFIGS = {
    "defaults": {},
    "no-quality": {"QUALITY_SIGNALS": "false"},
    "drop": {"MARKETPLACES": "acme=./mp"},
    "label": {"MARKETPLACES": "acme=./mp", "FILTER_MODE": "label"},
    "drop-extra": {"MARKETPLACES": "acme=./mp", "EXTRA_PLUGIN_ALLOWLIST": "other.plugin", "QUALITY_SIGNALS": "false"},
    "tls": {"PROMETHEUS_REMOTE_WRITE_URL": "https://prom.example.com/api/v1/write", "PROMETHEUS_TLS_ENABLED": "true",
            "PROMETHEUS_INSECURE_SKIP_VERIFY": "true"},
}


@pytest.mark.parametrize("env", CONFIGS.values(), ids=CONFIGS.keys())
def test_every_referenced_component_is_defined(make_config, env):
    doc = rendered(make_config(**env))
    defined = {kind: set(doc.get(kind) or {}) for kind in ("receivers", "processors", "exporters", "connectors",
                                                           "extensions")}
    assert set(doc["service"]["extensions"]) <= defined["extensions"]
    for name, pipeline in doc["service"]["pipelines"].items():
        assert set(pipeline["receivers"]) <= defined["receivers"] | defined["connectors"], name
        assert set(pipeline["processors"]) <= defined["processors"], name
        assert set(pipeline["exporters"]) <= defined["exporters"] | defined["connectors"], name
        assert pipeline["processors"][0] == "memory_limiter", f"{name}: memory_limiter must run first"
        assert pipeline["processors"][-1] == "batch" or name == "logs/events", name


def test_without_marketplaces_everything_passes_through(make_config):
    doc = rendered(make_config(QUALITY_SIGNALS="false"))
    assert doc["service"]["pipelines"]["metrics"]["processors"] == [*render.BASE_PROCESSORS, "batch"]
    assert "connectors" not in doc
    assert set(doc["service"]["pipelines"]) == {"metrics"}
    assert not any(p.endswith("/marketplace") for p in doc["processors"])


def test_secrets_are_never_inlined(make_config):
    cfg = make_config()
    text = render.render_collector_config(cfg, {})
    assert cfg.password not in text
    assert "${env:PROMETHEUS_PASSWORD}" in text
    assert "${env:PROMETHEUS_REMOTE_WRITE_URL}" in text
    assert "user.email" in text, "the privacy processor must drop user.email"


def test_drop_mode_filters_unmatched_datapoints(make_config):
    doc = rendered(make_config(MARKETPLACES="acme=./mp", QUALITY_SIGNALS="false"))
    assert doc["service"]["pipelines"]["metrics"]["processors"] == [
        *render.BASE_PROCESSORS, "transform/marketplace", "filter/marketplace", "batch"]
    [condition] = doc["processors"]["filter/marketplace"]["metrics"]["datapoint"]
    assert condition == 'attributes["marketplace"] == nil'


def test_label_mode_keeps_unmatched_datapoints(make_config):
    doc = rendered(make_config(MARKETPLACES="acme=./mp", FILTER_MODE="label", QUALITY_SIGNALS="false"))
    assert "filter/marketplace" not in doc["processors"]
    assert "transform/marketplace" in doc["service"]["pipelines"]["metrics"]["processors"]


def test_extra_plugins_survive_the_filter(make_config):
    cfg = make_config(MARKETPLACES="acme=./mp", EXTRA_PLUGIN_ALLOWLIST="other.plugin,zed")
    cond = render.unmatched_condition(cfg)
    assert cond.startswith('attributes["marketplace"] == nil and (attributes["plugin.name"] == nil or not IsMatch(')
    assert r"^(other\\.plugin|zed)$" in cond


def test_marketplace_statements(make_config):
    cfg = make_config(MARKETPLACES="acme=./mp")
    statements = render.marketplace_statements(cfg, ALLOWLIST)
    # p1 has components: label + plugin attribution. p2 has none: label by plugin name only.
    assert len(statements) == 3
    label_p1, attribute_p1, label_p2 = statements
    assert label_p1.startswith('set(attributes["marketplace"], "acme") where attributes["marketplace"] == nil')
    assert 'attributes["plugin.name"] == "p1"' in label_p1
    assert r'"^(s\\.two|s1)$"' in label_p1, "skill names are escaped and sorted"
    assert r'"^(reviewer)$"' in label_p1
    assert attribute_p1.startswith('set(attributes["plugin.name"], "p1") where attributes["marketplace"] == "acme"')
    assert "IsMatch" not in label_p2
    # Events also match agents by agent_type (subagent_completed).
    event_statements = render.marketplace_statements(cfg, ALLOWLIST, ("agent.name", "agent_type"))
    assert 'IsMatch(attributes["agent_type"], "^(reviewer)$")' in event_statements[0]


def test_unsynced_marketplace_renders_without_statements(make_config):
    doc = rendered(make_config(MARKETPLACES="acme=./mp", QUALITY_SIGNALS="false"), allowlist={})
    assert doc["processors"]["transform/marketplace"]["metric_statements"] == []


def test_exporter_tls(make_config, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy")
    cfg = make_config(PROMETHEUS_REMOTE_WRITE_URL="https://prom.example.com/api/v1/write",
                      PROMETHEUS_TLS_ENABLED="true", PROMETHEUS_CA_FILE=str(ca))
    tls = rendered(cfg)["exporters"]["prometheus_remote_write"]["http"]["tls"]
    assert tls == {"insecure_skip_verify": False, "ca_file": "/etc/otelcol/ca.pem"}
    assert "tls" not in rendered(make_config())["exporters"]["prometheus_remote_write"]["http"]


def test_wal_dir(make_config):
    doc = rendered(make_config(COLLECTOR_WAL_DIR="/data/my wal"))
    assert doc["exporters"]["prometheus_remote_write"]["wal"]["directory"] == "/data/my wal"


def test_unrendered_markers_are_an_error(make_config):
    with pytest.raises(ValueError, match="@@NEW_MARKER@@"):
        render.render_collector_config(make_config(), {}, template="a: @@NEW_MARKER@@\n")


# --- quality signals (events) --------------------------------------------------------------

def test_events_are_never_exported(make_config):
    doc = rendered(make_config(MARKETPLACES="acme=./mp"))
    pipelines = doc["service"]["pipelines"]
    assert pipelines["logs/events"]["exporters"] == ["signal_to_metrics"]
    assert pipelines["metrics/events"]["receivers"] == ["signal_to_metrics"]
    assert "delta_to_cumulative" in pipelines["metrics/events"]["processors"]
    assert not any(name.startswith("logs") for name in doc["exporters"])


def test_only_safe_event_attributes_become_labels(make_config):
    doc = rendered(make_config())
    metrics = doc["connectors"]["signal_to_metrics"]["logs"]
    assert {m["name"] for m in metrics} >= {"claude_code.skill.activations", "claude_code.tool.results"}
    for metric in metrics:
        keys = {a["key"] for a in metric["attributes"]}
        assert keys <= SAFE_EVENT_LABELS, f"{metric['name']}: unexpected labels {keys - SAFE_EVENT_LABELS}"
        assert len(metric["conditions"]) == 1 and "event.name" in metric["conditions"][0]


def test_event_processors_in_drop_mode(make_config):
    block, ids = events.processors_block(make_config(MARKETPLACES="acme=./mp"), ALLOWLIST)
    assert ids == [*events.LOG_PROCESSORS, "transform/marketplace-events", "filter/marketplace-events",
                   "transform/strip-unmatched-events"]
    doc = yaml.safe_load("processors:\n" + block)["processors"]
    [cond] = doc["filter/marketplace-events"]["logs"]["log_record"]
    assert "skill_activated|subagent_completed" in cond
    strip = doc["transform/strip-unmatched-events"]["log_statements"][0]["statements"]
    assert [s.split('"')[1] for s in strip] == ["skill.name", "agent.name", "plugin.name"]


def test_event_processors_in_label_mode_and_without_marketplaces(make_config):
    _, ids = events.processors_block(make_config(MARKETPLACES="acme=./mp", FILTER_MODE="label"), ALLOWLIST)
    assert ids == [*events.LOG_PROCESSORS, "transform/marketplace-events"]
    _, ids = events.processors_block(make_config(), ALLOWLIST)
    assert ids == events.LOG_PROCESSORS
