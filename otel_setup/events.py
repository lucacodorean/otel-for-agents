"""Quality signals: turn Claude Code *events* (OTLP logs) into Prometheus metrics.

Claude Code reports skill activations, tool results, subagent completions and hook
runs only as log events. The logs pipeline below reads them from the same OTLP
receiver and feeds the `signal_to_metrics` connector. **No logs exporter is
configured, so no event is ever stored or exported**: only the attributes named in
the connector config survive, as metric labels.

Events carry tool details (Bash commands, file paths, error text, tool input) when
OTEL_LOG_TOOL_DETAILS=1. None of those attributes is ever listed as a connector
attribute, so none can reach Prometheus.

The connector emits delta metrics, which Prometheus remote write cannot ingest, so the
events metrics pipeline converts them to cumulative with `delta_to_cumulative`.
"""
from __future__ import annotations

from otel_setup.config import Config
from otel_setup.render import _q, _transform_block, marketplace_statements, unmatched_condition

# Events that are about a marketplace component (skill / agent / plugin) and are dropped in
# drop mode when they match none. Everything else (tool results, hooks, API errors) is kept
# as a global counter; those that name a matching component also get its labels.
SCOPED_EVENTS = ("skill_activated", "subagent_completed")

LOG_PROCESSORS = ["memory_limiter", "resource_detection", "attributes/privacy", "transform/events"]

# tool_result carries no agent or skill, but a Skill / Agent tool call names its target inside
# the JSON `tool_parameters` string. Lift just that one field into the attribute the rest of
# the pipeline (marketplace labelling, the connector) already understands. Nothing else of
# tool_parameters is read, and the attribute itself is never a connector label.
LIFT_STATEMENTS = (
    'set(attributes["skill.name"], ParseJSON(attributes["tool_parameters"])["skill_name"]) '
    'where attributes["event.name"] == "tool_result" and attributes["tool_name"] == "Skill" '
    'and attributes["tool_parameters"] != nil and attributes["skill.name"] == nil',
    'set(attributes["agent.name"], ParseJSON(attributes["tool_parameters"])["subagent_type"]) '
    'where attributes["event.name"] == "tool_result" and attributes["tool_name"] != nil and IsMatch(attributes["tool_name"], "^(Agent|Task)$") '
    'and attributes["tool_parameters"] != nil and attributes["agent.name"] == nil',
)

TOOL_BUCKETS_MS = (10, 50, 100, 500, 1000, 5000, 15000, 60000)
SUBAGENT_BUCKETS_MS = (1000, 5000, 15000, 30000, 60000, 120000, 300000, 600000)


def _event(name: str) -> str:
    return f'log.attributes["event.name"] == "{name}"'


def _value(attr: str) -> str:
    # Claude Code may send numbers as numbers or as strings; Double() accepts both.
    return f'Double(log.attributes["{attr}"])'


def _metric(name: str, event: str, description: str, kind: dict, required: tuple[str, ...],
            optional: tuple[str, ...] = ()) -> list[str]:
    """One signal_to_metrics entry. Optional attributes become labels only when present."""
    lines = [f"      - name: {name}", f"        description: {_q(description)}",
             "        conditions:", f"          - {_q(_event(event))}", "        attributes:"]
    lines += [f"          - key: {k}" for k in required]
    lines += [f"          - key: {k}\n            optional: true" for k in optional]
    if "buckets" in kind:
        lines += ["        histogram:", f"          buckets: [{', '.join(str(b) for b in kind['buckets'])}]",
                  f"          value: {_q(kind['value'])}"]
    else:
        lines += ["        sum:", f"          value: {_q(kind['value'])}", "          monotonic: true"]
    return lines


COUNT = {"value": "1"}


def connectors_block() -> str:
    m: list[str] = []
    skill = (("skill.name",), ("plugin.name", "marketplace", "invocation_trigger", "skill.source"))
    sub = (("agent_type",), ("plugin.name", "marketplace", "is_async", "model"))
    sub_short = (("agent_type",), ("plugin.name", "marketplace"))
    # skill.name / agent.name / plugin.name / marketplace are only set for Skill and Agent tool
    # calls (see LIFT_STATEMENTS), so only those results get a component label.
    tool = (("tool_name", "success"), ("error_type", "skill.name", "agent.name", "plugin.name", "marketplace"))
    api = (("model",), ("status_code", "agent.name", "skill.name", "plugin.name", "marketplace"))
    api_exhausted = (("model",), ("status_code",))
    hook = (("hook_event",), ("hook_name",))

    m += _metric("claude_code.skill.activations", "skill_activated", "Skill activations", COUNT, *skill)
    m += _metric("claude_code.subagent.completions", "subagent_completed", "Subagent runs completed", COUNT, *sub)
    m += _metric("claude_code.subagent.tool_uses", "subagent_completed", "Tool calls made by subagents",
                 {"value": _value("total_tool_uses")}, *sub)
    m += _metric("claude_code.subagent.duration.ms", "subagent_completed", "Subagent run time in ms",
                 {"buckets": SUBAGENT_BUCKETS_MS, "value": _value("duration_ms")}, *sub_short)
    m += _metric("claude_code.tool.results", "tool_result", "Tool calls that ran, by outcome", COUNT, *tool)
    m += _metric("claude_code.tool.duration.ms", "tool_result", "Tool run time in ms",
                 {"buckets": TOOL_BUCKETS_MS, "value": _value("duration_ms")}, ("tool_name", "success"))
    m += _metric("claude_code.api.errors", "api_error", "Failed API requests, with the agent/skill that issued them", COUNT, *api)
    m += _metric("claude_code.api.retries_exhausted", "api_retries_exhausted",
                 "API requests that failed after more than one attempt", COUNT, *api_exhausted)
    m += _metric("claude_code.hook.executions", "hook_execution_complete", "Hook runs", COUNT, *hook)
    m += _metric("claude_code.hook.blocking", "hook_execution_complete",
                 "Hook commands that returned a blocking decision", {"value": _value("num_blocking")}, *hook)
    m += _metric("claude_code.hook.errors", "hook_execution_complete",
                 "Hook commands that failed without blocking", {"value": _value("num_non_blocking_error")}, *hook)
    m += _metric("claude_code.hook.duration.ms", "hook_execution_complete", "Hook run time in ms",
                 {"buckets": TOOL_BUCKETS_MS, "value": _value("total_duration_ms")}, *hook)
    return "connectors:\n  signal_to_metrics:\n    logs:\n" + "\n".join(m) + "\n"


def processors_block(cfg: Config, allowlist: dict) -> tuple[str, list[str]]:
    """(YAML, ids) for the logs pipeline processors beyond the shared base ones, plus the
    processor that makes the connector's delta metrics cumulative."""
    lines = _transform_block("transform/events", "log_statements", "log", list(LIFT_STATEMENTS))
    lines += ["  delta_to_cumulative:", "    max_stale: 8760h   # effectively never expire: counters live as long as the Collector"]
    ids = list(LOG_PROCESSORS)
    if cfg.marketplaces:
        lines += _transform_block("transform/marketplace-events", "log_statements", "log",
                                  marketplace_statements(cfg, allowlist, ("agent.name", "agent_type")))
        ids.append("transform/marketplace-events")
        if cfg.filter_mode == "drop":
            # Scoped events that matched no marketplace are dropped; tool and hook events,
            # which name no component, stay as global counters.
            scoped = "|".join(SCOPED_EVENTS)
            cond = (f'IsMatch(attributes["event.name"], "^({scoped})$") and '
                    f'({unmatched_condition(cfg)})')
            # Kept events that matched no marketplace (global counters) must not carry the names
            # of skills, agents or plugins that are not yours.
            strip = [f'delete_key(attributes, "{k}") where {unmatched_condition(cfg)}'
                     for k in ("skill.name", "agent.name", "plugin.name")]
            lines += _transform_block("transform/strip-unmatched-events", "log_statements", "log", strip)
            lines += ["  filter/marketplace-events:", "    error_mode: ignore", "    logs:", "      log_record:",
                      f"        - {_q(cond)}"]
            ids.append("filter/marketplace-events")
            ids.append("transform/strip-unmatched-events")
    return "\n".join(lines) + "\n", ids


def pipelines_block(processor_ids: list[str]) -> str:
    # Both pipelines share the single OTLP receiver and the single exporter. The events
    # metrics pipeline has no marketplace filter on purpose: scoping was done on the logs.
    return (
        "    logs/events:\n"
        "      receivers: [otlp]\n"
        f"      processors: [{', '.join(processor_ids)}]\n"
        "      exporters: [signal_to_metrics]   # no logs exporter: events are never stored\n"
        "    metrics/events:\n"
        "      receivers: [signal_to_metrics]\n"
        "      processors: [memory_limiter, delta_to_cumulative, batch]\n"
        "      exporters: [prometheus_remote_write]\n"
    )
