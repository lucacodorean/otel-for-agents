"""The rendered Collector config against the real (pinned) Collector image.

`validate` catches config the Collector rejects; the pipeline test sends OTLP metrics and
events through a Collector into the dev Prometheus and checks what arrives.
"""
from __future__ import annotations

import json
import time

import pytest

from otel_setup import prometheus as prom_helpers
from otel_setup.render import render_collector_config, render_prometheus_files
from tests.support import BASE_ENV, COLLECTOR_IMAGE, PROMETHEUS_IMAGE, REPO, wait_for

ALLOWLIST = {"acme": {"p1": {"skills": ["s1"], "agents": ["reviewer"]}}}
CONFIG_PATH = "/etc/otelcol-contrib"  # exists in the image: docker cp cannot create parents
COLLECTOR_ENV = {k: v for k, v in BASE_ENV.items()}

VARIANTS = {
    "defaults": {},
    "no-quality": {"QUALITY_SIGNALS": "false"},
    "drop": {"MARKETPLACES": "acme=./mp", "EXTRA_PLUGIN_ALLOWLIST": "extra"},
    "label": {"MARKETPLACES": "acme=./mp", "FILTER_MODE": "label"},
    "tls": {"PROMETHEUS_REMOTE_WRITE_URL": "https://prom.example.com/api/v1/write", "PROMETHEUS_TLS_ENABLED": "true",
            "PROMETHEUS_INSECURE_SKIP_VERIFY": "true"},
}


@pytest.mark.parametrize("env", VARIANTS.values(), ids=VARIANTS.keys())
def test_collector_accepts_rendered_config(docker, make_config, env):
    cfg = make_config(**env)
    code, out = docker.run(
        COLLECTOR_IMAGE, "validate", f"--config={CONFIG_PATH}/config.yaml",
        env={**COLLECTOR_ENV, "PROMETHEUS_REMOTE_WRITE_URL": cfg.remote_write_url},
        files={CONFIG_PATH: {"config.yaml": render_collector_config(cfg, ALLOWLIST)}})
    assert code == 0, out


# --- end to end ----------------------------------------------------------------------------

def _attrs(**values) -> list[dict]:
    return [{"key": k.replace("__", "."), "value": {"stringValue": v}} for k, v in values.items()]


def otlp_metrics(now_ns: int) -> str:
    def point(value, **attrs):
        return {"asDouble": value, "startTimeUnixNano": str(now_ns - 60 * 10**9), "timeUnixNano": str(now_ns),
                "attributes": _attrs(type="input", session__id="sess-1", user__email="someone@example.com", **attrs)}
    return json.dumps({"resourceMetrics": [{
        # Claude Code sends user.email on each data point, where attributes/privacy deletes it.
        "resource": {"attributes": _attrs(service__name="claude-code")},
        "scopeMetrics": [{"scope": {"name": "com.anthropic.claude_code"}, "metrics": [{
            "name": "claude_code.token.usage", "unit": "tokens",
            "sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [
                point(100, skill__name="s1"),           # a skill of plugin p1 in marketplace acme
                point(7, skill__name="someone-else"),   # matches no marketplace: dropped
            ]}}]}]}]})


def otlp_events(now_ns: int) -> str:
    def event(name, **attrs):
        return {"timeUnixNano": str(now_ns), "body": {"stringValue": f"claude_code.{name}"},
                "attributes": _attrs(event__name=name, **attrs)}
    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": _attrs(service__name="claude-code")},
        "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code.events"}, "logRecords": [
            event("skill_activated", skill__name="s1", invocation_trigger="user-slash"),
            event("skill_activated", skill__name="someone-else"),
            event("tool_result", tool_name="Bash", success="true", duration_ms="12",
                  tool_parameters='{"command": "cat /secret"}'),
        ]}]}]})


@pytest.fixture(scope="module")
def pipeline(docker, tmp_path_factory):
    """Dev Prometheus + Collector (drop mode, quality signals) on one network, and curl."""
    from otel_setup.config import build_config
    cfg = build_config({**BASE_ENV, "MARKETPLACES": "acme=./mp"})
    net = docker.network()

    prom_files = render_prometheus_files(cfg, prom_helpers.hash_password(cfg.password))
    prom_files["recording-rules.yml"] = REPO / "infra" / "prometheus" / "recording-rules.yml"
    prom = docker.create(
        PROMETHEUS_IMAGE, "--config.file=/etc/prometheus/prometheus.yml",
        "--web.config.file=/etc/prometheus/web-config.yml", "--web.enable-remote-write-receiver",
        "--storage.tsdb.path=/prometheus", network=net, aliases=("prometheus",),
        files={"/etc/prometheus": prom_files})
    docker.start(prom)

    col = docker.create(
        COLLECTOR_IMAGE, f"--config={CONFIG_PATH}/config.yaml", env=COLLECTOR_ENV, network=net,
        aliases=("collector",), hostname="ci-host", tmpfs=("/var/lib/otelcol:mode=1777",),
        files={CONFIG_PATH: {"config.yaml": render_collector_config(cfg, ALLOWLIST)}})
    docker.start(col)

    http = docker.http(net)
    auth = ("-u", f"{cfg.username}:{cfg.password}")
    wait_for(lambda: http.request("http://prometheus:9090/-/ready", *auth)[0] == 200, 90,
             what=f"Prometheus ready\n{docker.logs(prom)}")
    wait_for(lambda: http.request("http://collector:13133/")[0] == 200, 90,
             what=f"Collector healthy\n{docker.logs(col)}")

    now = time.time_ns()
    for path, body in (("/v1/metrics", otlp_metrics(now)), ("/v1/logs", otlp_events(now))):
        status, text = http.request(f"http://collector:4318{path}", "-H", "Content-Type: application/json",
                                    data=body)
        assert status == 200, f"{path}: HTTP {status} {text}"

    def query(promql: str) -> list[dict]:
        status, body = http.request("http://prometheus:9090/api/v1/query", *auth, "-G",
                                    "--data-urlencode", f"query={promql}")
        assert status == 200, body
        return json.loads(body)["data"]["result"]

    def wait_series(promql: str) -> list[dict]:
        return wait_for(lambda: query(promql), 150, 3,
                        what=f"{promql}\n--- collector logs ---\n{docker.logs(col)[-4000:]}")

    return query, wait_series


def test_metrics_reach_prometheus_with_marketplace_labels(pipeline):
    query, wait_series = pipeline
    [series] = wait_series('claude_code_token_usage_tokens_total{skill_name="s1"}')
    labels = series["metric"]
    assert (labels["marketplace"], labels["plugin_name"]) == ("acme", "p1")
    assert labels["session_id"] == "sess-1"
    assert float(series["value"][1]) == 100
    assert "user_email" not in labels, "the privacy processor drops user.email"


def test_unmatched_metrics_are_dropped(pipeline):
    query, wait_series = pipeline
    wait_series('claude_code_token_usage_tokens_total{skill_name="s1"}')  # same batch has arrived
    assert query('claude_code_token_usage_tokens_total{skill_name="someone-else"}') == []


def test_events_become_counters(pipeline):
    query, wait_series = pipeline
    [skill] = wait_series('claude_code_skill_activations_total{skill_name="s1"}')
    assert skill["metric"]["marketplace"] == "acme"
    assert skill["metric"]["invocation_trigger"] == "user-slash"
    assert query('claude_code_skill_activations_total{skill_name="someone-else"}') == []
    [tool] = wait_series('claude_code_tool_results_total{tool_name="Bash"}')
    assert not any("tool_parameters" in k or "secret" in v for k, v in tool["metric"].items())
