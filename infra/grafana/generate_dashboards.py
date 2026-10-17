#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards (standard library only).

    python3 infra/grafana/generate_dashboards.py

Writes dashboards/*.json. The JSON files are committed; edit this script and re-run it
instead of editing them by hand (provisioned dashboards are read-only in Grafana).

Every panel queries the recording rules from infra/prometheus/recording-rules.yml, never the
raw per-session counters, so the dashboards stay cheap and exact (see the README, "Recording
rules"). Delta rules are evaluated every 5 minutes, so time series use a 5m minimum interval.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"

# Sections: each becomes a Grafana folder (the sub-directory name is the folder name, see
# provisioning/dashboards/claude-code.yml) and a dropdown in every dashboard's links menu.
SECTIONS = {"general": "Claude Code - General", "detail": "Claude Code - Agents and skills"}
DS = {"type": "prometheus", "uid": "prometheus"}

# Selector shared by the per-component rules; "All" uses ".*" so series without the label match.
SEL = '{marketplace=~"$marketplace",plugin_name=~"$plugin"}'
MP = '{marketplace=~"$marketplace"}'


# Dropdown sources. Each is an instant query evaluated at the end of the time range, so every source
# looks back over the whole range with count_over_time. Several sources are combined so the list also
# holds components with events but no consumption yet (and the other way round).
AGENTS_IN_RANGE = (
    "query_result("
    'count by (agent_name) (count_over_time(claude_code:token_usage_by_component:delta5m{agent_name!=""}[$__range])) '
    'or label_replace(count by (agent_type) (count_over_time(claude_code:subagent_completions_by_agent:delta5m{agent_type!=""}[$__range])), '
    '"agent_name", "$1", "agent_type", "(.+)")'
    ")")
SKILLS_IN_RANGE = (
    "query_result("
    'count by (skill_name) (count_over_time(claude_code:token_usage_by_component:delta5m{skill_name!=""}[$__range])) '
    'or count by (skill_name) (count_over_time(claude_code:skill_activations_by_skill:delta5m{skill_name!=""}[$__range]))'
    ")")


class Board:
    def __init__(self, uid: str, title: str, description: str, with_plugin: bool = True, section: str = "general", single: tuple[str, str, str, str] | None = None):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self._id = 0
        self._y = 0
        self._x = 0
        self._row_h = 0
        self.with_plugin = with_plugin
        self.section = section
        self.single = single  # (variable, label, query, regex): a single-select variable instead of marketplace/plugin

    # --- layout -------------------------------------------------------------------------
    def _place(self, w: int, h: int) -> dict:
        if self._x + w > 24:
            self._y += self._row_h
            self._x, self._row_h = 0, 0
        pos = {"x": self._x, "y": self._y, "w": w, "h": h}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def row(self, title: str) -> None:
        self._y += self._row_h
        self._x, self._row_h = 0, 0
        self._id += 1
        self.panels.append({"id": self._id, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"x": 0, "y": self._y, "w": 24, "h": 1}, "panels": []})
        self._y += 1

    def _panel(self, kind: str, title: str, targets: list[dict], w: int, h: int, description: str,
               defaults: dict, options: dict | None = None, **extra) -> None:
        self._id += 1
        panel = {"id": self._id, "type": kind, "title": title, "description": description,
                 "datasource": DS, "gridPos": self._place(w, h), "targets": targets,
                 "fieldConfig": {"defaults": defaults, "overrides": []}}
        if options:
            panel["options"] = options
        panel.update(extra)
        self.panels.append(panel)

    # --- panel kinds --------------------------------------------------------------------
    def stat(self, title, expr, unit="short", w=4, h=4, description="", decimals=None, thresholds=None):
        d = {"unit": unit, "color": {"mode": "thresholds"},
             "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "blue", "value": None}]}}
        if decimals is not None:
            d["decimals"] = decimals
        self._panel("stat", title, [target(expr, instant=True)], w, h, description, d,
                    {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"})

    def series(self, title, exprs, unit="short", w=12, h=8, description="", bars=True, stack=True,
               interval="5m"):
        """`exprs`: a string, or a list of (expr, legend)."""
        if isinstance(exprs, str):
            exprs = [(exprs, "{{__name__}}")]
        targets = [target(e, legend=l, interval=interval) for e, l in exprs]
        custom = {"drawStyle": "bars" if bars else "line", "fillOpacity": 70 if bars else 10,
                  "lineWidth": 1, "showPoints": "never", "spanNulls": True,
                  "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
        self._panel("timeseries", title, targets, w, h, description,
                    {"unit": unit, "custom": custom},
                    {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["sum" if bars else "lastNotNull"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}},
                    interval=interval)

    def bars(self, title, expr, legend, unit="short", w=12, h=8, description="", maximum=None, top=None):
        d = {"unit": unit, "color": {"mode": "palette-classic"}}
        if maximum is not None:
            d.update({"min": 0, "max": maximum})
        e = f"topk({top}, {expr})" if top else expr
        self._panel("bargauge", title, [target(e, legend=legend, instant=True)], w, h, description, d,
                    {"displayMode": "gradient", "orientation": "horizontal", "reduceOptions": {"calcs": ["lastNotNull"]},
                     "showUnfilled": True, "minVizHeight": 16})

    def text(self, content: str, w=24, h=3) -> None:
        self._id += 1
        self.panels.append({"id": self._id, "type": "text", "title": "", "gridPos": self._place(w, h),
                            "options": {"mode": "markdown", "content": content}})

    # --- output -------------------------------------------------------------------------
    def variables(self) -> list[dict]:
        if self.single:
            # One agent or skill, chosen from those that have data in the dashboard's time range.
            name, label, q, regex = self.single
            return [{"name": name, "label": label, "type": "query", "datasource": DS, "multi": False,
                     "includeAll": False, "refresh": 2, "sort": 1, "definition": q, "regex": regex,
                     "query": {"query": q, "refId": "v"}}]
        def var(name, label, query):
            return {"name": name, "label": label, "type": "query", "datasource": DS, "multi": True,
                    "includeAll": True, "allValue": ".*", "refresh": 2, "sort": 1,
                    "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
                    "definition": query, "query": {"query": query, "refId": "v"}}
        v = [var("marketplace", "Marketplace", "label_values(claude_code:token_usage_by_component:delta5m, marketplace)")]
        if self.with_plugin:
            v.append(var("plugin", "Plugin",
                         'label_values(claude_code:token_usage_by_component:delta5m{marketplace=~"$marketplace"}, plugin_name)'))
        return v

    def render(self) -> dict:
        return {"uid": self.uid, "title": self.title, "description": self.description,
                "tags": ["claude-code", "generated", f"cc-{self.section}"], "schemaVersion": 39, "version": 1,
                "editable": False, "graphTooltip": 1, "refresh": "1m",
                "time": {"from": "now-24h", "to": "now"}, "timezone": "browser",
                "templating": {"list": self.variables()}, "annotations": {"list": []},
                "links": [
                    {"title": "General", "type": "dashboards", "tags": ["cc-general"], "asDropdown": True,
                     "includeVars": True, "keepTime": True},
                    {"title": "Agents and skills", "type": "dashboards", "tags": ["cc-detail"], "asDropdown": True,
                     "includeVars": False, "keepTime": True}],
                "panels": self.panels}

    def write(self) -> None:
        folder = OUT / SECTIONS[self.section]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{self.uid}.json").write_text(json.dumps(self.render(), indent=2, sort_keys=True) + "\n")


def target(expr: str, legend: str = "", instant: bool = False, interval: str | None = None) -> dict:
    t = {"datasource": DS, "expr": expr, "refId": "A", "legendFormat": legend or "",
         "instant": instant, "range": not instant}
    if instant:
        t["format"] = "time_series"
    if interval:
        t["interval"] = interval
    return t


def named(expr: str) -> str:
    """Add a `component` label: the skill and/or agent name, or "main session" for usage that
    belongs to neither (the main session's own turns). Use "{{component}}" as the legend."""
    joined = f'label_join({expr}, "component", "", "skill_name", "agent_name")'
    return f'label_replace({joined}, "component", "main session", "component", "")'


def in_range(rule: str, sel: str) -> str:
    """Exact total over the dashboard range: sum of the non-overlapping 5m deltas."""
    return f"sum(sum_over_time({rule}{sel}[$__range]))"


# =======================================================================================
def consumption() -> Board:
    b = Board("cc-consumption", "Claude Code: consumption",
              "Tokens and cost per agent, skill, plugin, model and host. Exact totals from the "
              "*:delta5m recording rules (see the README).")
    tok, cost = "claude_code:token_usage_by_component:delta5m", "claude_code:cost_usage_by_component:delta5m"

    b.row("Totals in the selected time range")
    b.stat("Cost", in_range(cost, SEL), "currencyUSD", description="Sum of all 5-minute cost deltas in range.", decimals=2)
    b.stat("Tokens", in_range(tok, SEL), "short", description="All token types.")
    b.stat("USD per million tokens", f"{in_range(cost, SEL)} / ({in_range(tok, SEL)} / 1e6)", "currencyUSD", decimals=2,
           description="Cost divided by tokens, all token types.")
    cache = 'claude_code:token_usage:delta5m{marketplace=~"$marketplace",plugin_name=~"$plugin",type="cacheRead"}'
    allin = 'claude_code:token_usage:delta5m{marketplace=~"$marketplace",plugin_name=~"$plugin",type=~"input|cacheRead|cacheCreation"}'
    b.stat("Cache hit ratio", f"sum(sum_over_time({cache}[$__range])) / sum(sum_over_time({allin}[$__range]))", "percentunit", decimals=0,
           description="cacheRead / (input + cacheRead + cacheCreation).")
    b.stat("Active sessions (last hour)",
           f"clamp_max(sum(claude_code:active_sessions:count1h{MP}), scalar(sum(claude_code:active_sessions_by_host:count1h)))", "short",
           description="Distinct sessions with token usage in the last hour, in the selected marketplaces (\"none\" = "
                       "outside any configured marketplace). A session that used several selected marketplaces can count "
                       "more than once, but never beyond the total number of sessions.")
    b.stat("Hosts", f"count(count by (host_name) (claude_code:token_usage_by_host:delta5m{MP}))", "short",
           description="Hosts with usage in the last 5 minutes window of the rules.")

    b.row("Over time")
    b.series("Tokens per agent / skill", [(named(f"sum by (agent_name, skill_name) ({tok}{SEL})"), "{{component}}")],
             description="Consumption in each 5-minute step, stacked. Sum over a period for exact totals. "
                         "\"main session\" = usage outside any agent or skill.")
    b.series("Cost per agent / skill", [(named(f"sum by (agent_name, skill_name) ({cost}{SEL})"), "{{component}}")], "currencyUSD",
             description="Cost in each 5-minute step, stacked.")
    cum = "claude_code:token_usage_by_component:cumulative"
    b.series("Cumulative tokens since the start of the range",
             [(named(f"sum by (agent_name, skill_name) ({cum}{SEL}) - (sum by (agent_name, skill_name) (last_over_time({cum}{SEL}[1h] @ start())) or (0 * sum by (agent_name, skill_name) ({cum}{SEL})))"),
               "{{component}}")],
             bars=False, stack=False, interval="5m",
             description="Running total per agent/skill, zero at the left edge of the range (uses the @ start() modifier).")
    ccum = "claude_code:cost_usage_by_component:cumulative"
    b.series("Cumulative cost since the start of the range",
             [(named(f"sum by (agent_name, skill_name) ({ccum}{SEL}) - (sum by (agent_name, skill_name) (last_over_time({ccum}{SEL}[1h] @ start())) or (0 * sum by (agent_name, skill_name) ({ccum}{SEL})))"),
               "{{component}}")],
             "currencyUSD", bars=False, stack=False, description="Same, for cost.")
    b.series("Tokens by type", [(f'sum by (type) (claude_code:token_usage_by_model:delta5m{MP})', "{{type}}")],
             description="input / output / cacheRead / cacheCreation.")
    b.series("Cost per model", [(f'sum by (model) (claude_code:cost_usage_by_model:delta5m{MP})', "{{model}}")], "currencyUSD")
    b.series("Tokens per host", [(f'sum by (host_name) (claude_code:token_usage_by_host:delta5m{MP})', "{{host_name}}")])
    b.series("Cost per host", [(f'sum by (host_name) (claude_code:cost_usage_by_host:delta5m{MP})', "{{host_name}}")], "currencyUSD")

    b.row("Rankings in the selected time range")
    by = "marketplace, plugin_name, skill_name, agent_name"
    b.bars("Top agents / skills by cost", named(f"sum by ({by}) (sum_over_time({cost}{SEL}[$__range]))"),
           "{{plugin_name}} {{component}}", "currencyUSD", top=15)
    b.bars("Top agents / skills by tokens", named(f"sum by ({by}) (sum_over_time({tok}{SEL}[$__range]))"),
           "{{plugin_name}} {{component}}", "short", top=15)
    b.bars("Cache hit ratio, last hour", named(f"claude_code:cache_hit_ratio:1h{SEL}"), "{{plugin_name}} {{component}} {{model}}",
           "percentunit", maximum=1, top=15, description="Per agent/skill and model.")
    b.bars("USD per million tokens, last 24 h", named(f"claude_code:cost_per_mtok:24h{SEL}"), "{{plugin_name}} {{component}}",
           "currencyUSD", top=15)
    return b


def quality() -> Board:
    b = Board("cc-quality", "Claude Code: quality signals",
              "Skill activations, subagent runs, tool outcomes and hooks, from Claude Code events "
              "(QUALITY_SIGNALS=true). Tool and hook panels are global: those events name no component.",
              with_plugin=False)
    act, subs = "claude_code:skill_activations_by_skill:delta5m", "claude_code:subagent_completions_by_agent:delta5m"

    b.row("Totals in the selected time range")
    b.stat("Skill activations", in_range(act, MP), description="Skills activated.")
    b.stat("Subagent runs", in_range(subs, MP), description="Subagents completed.")
    b.stat("Tool calls", "sum(sum_over_time(claude_code:tool_results_by_tool:delta5m[$__range]))", description="All tools, global.")
    b.stat("Tool success rate, 24 h",
           'sum(claude_code:tool_results_by_tool:delta24h{success="true"}) / sum(claude_code:tool_results_by_tool:delta24h)',
           "percentunit", decimals=1, description="Share of tool calls that succeeded, all tools.",
           thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.8}, {"color": "green", "value": 0.95}])
    b.stat("Hook runs", "sum(sum_over_time(claude_code:hook_executions:delta5m[$__range]))", description="Global.")
    b.stat("Hook blocks", "sum(sum_over_time(claude_code:hook_blocking:delta5m[$__range]))", description="Hook commands that blocked.")

    b.row("Skills")
    b.series("Skill activations", [(f"sum by (skill_name) ({act}{MP})", "{{skill_name}}")],
             description="Activations per 5-minute step.")
    b.series("Activations by trigger", [(f"sum by (invocation_trigger) (claude_code:skill_activations_by_trigger:delta5m{MP})", "{{invocation_trigger}}")],
             description="user-slash: you ran it; claude-proactive: Claude chose it; nested-skill: another skill called it.")
    b.bars("Proactive share, last 24 h", f"claude_code:skill_proactive_share:24h{MP}", "{{skill_name}}", "percentunit", maximum=1,
           description="Share of a skill's activations that Claude chose itself. Low = people must call it by hand (discoverability).")
    b.bars("Top skills, last 24 h", f"sum by (skill_name) (claude_code:skill_activations_by_skill:delta24h{MP})", "{{skill_name}}", top=15)

    b.row("Subagents")
    b.series("Subagent runs", [(f"sum by (agent_type) ({subs}{MP})", "{{agent_type}}")])
    b.bars("Duration p95, last hour", f"claude_code:subagent_duration_seconds:p95_1h{MP}", "{{agent_type}}", "s", top=15)
    b.bars("Average duration, last hour", f"claude_code:subagent_duration_seconds:avg_1h{MP}", "{{agent_type}}", "s", top=15)
    b.bars("Tool calls per run, last 24 h", f"claude_code:subagent_tool_uses_per_run:24h{MP}", "{{agent_type}}", top=15,
           description="Average number of tool calls a subagent made per run.")

    b.row("Tools (global)")
    b.series("Tool calls by outcome", [("sum by (tool_name, success) (claude_code:tool_results_by_tool:delta5m)", "{{tool_name}} {{success}}")])
    b.bars("Tool success rate, last 24 h", "claude_code:tool_success_ratio:24h", "{{tool_name}}", "percentunit", maximum=1)
    b.bars("Tool failures by error type, last 24 h", "claude_code:tool_errors_by_type:delta24h", "{{tool_name}} {{error_type}}", top=15)
    b.bars("Tool duration p95, last hour", "claude_code:tool_duration_seconds:p95_1h", "{{tool_name}}", "s", top=15)

    b.row("Hooks (global)")
    b.bars("Hook failure ratio, last 24 h", "claude_code:hook_error_ratio:24h", "{{hook_event}} {{hook_name}}", "percentunit", maximum=1,
           description="Hook commands that failed without blocking, per hook run.")
    b.bars("Hook blocking ratio, last 24 h", "claude_code:hook_blocking_ratio:24h", "{{hook_event}} {{hook_name}}", "percentunit", maximum=1)
    b.bars("Average hook duration, last hour", "claude_code:hook_duration_seconds:avg_1h", "{{hook_event}} {{hook_name}}", "s", top=15)
    return b


def errors() -> Board:
    b = Board("cc-errors", "Claude Code: errors by agent and skill",
              "API errors and failed Skill / Agent calls attributed to the agent or skill involved. A failure "
              "inside a skill or agent (a Bash error, say) cannot be attributed: see the README.",
              with_plugin=False)
    api_c, fail = "claude_code:api_errors_by_component:delta5m", "claude_code:tool_failures_by_component:delta5m"

    b.row("Totals in the selected time range")
    b.stat("API errors (all)", "sum(sum_over_time(claude_code:api_errors_by_status:delta5m[$__range]))",
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 20}])
    b.stat("API errors with a component", in_range(api_c, MP), description="Errors issued by a named skill or agent.")
    b.stat("Failed skill / agent calls", in_range(fail, MP),
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 10}])

    b.row("API errors")
    b.series("API errors by status", [("sum by (model, status_code) (claude_code:api_errors_by_status:delta5m)", "{{model}} {{status_code}}")],
             description="All API errors, including the main session (no component).")
    b.series("API errors by agent / skill", [(f"sum by (agent_name, skill_name) ({api_c}{MP})", "{{agent_name}}{{skill_name}}")],
             description="Only errors that name an agent or skill.")
    b.bars("API errors by agent / skill, last 24 h", f"claude_code:api_errors_by_component:delta24h{MP}",
           "{{plugin_name}} {{skill_name}}{{agent_name}}", top=15)
    b.bars("API errors by status, last 24 h", "claude_code:api_errors_by_status:delta24h", "{{model}} {{status_code}}", top=15)

    b.row("Failed skill and agent invocations")
    b.series("Failed Skill / Agent calls", [(f"sum by (skill_name, agent_name, tool_name) ({fail}{MP})", "{{tool_name}} {{skill_name}}{{agent_name}}")],
             description="The invocation itself failed (skill not found, subagent errored).")
    b.bars("Failed calls by error type, last 24 h", f"claude_code:tool_failures_by_component:delta24h{MP}",
           "{{tool_name}} {{skill_name}}{{agent_name}} {{error_type}}", top=15)
    b.bars("Skill invocation success rate, last 24 h", f"claude_code:skill_invocation_success_ratio:24h{MP}", "{{skill_name}}",
           "percentunit", maximum=1)
    b.bars("Agent invocation success rate, last 24 h", f"claude_code:agent_invocation_success_ratio:24h{MP}", "{{agent_name}}",
           "percentunit", maximum=1)
    return b


def agent_detail() -> Board:
    b = Board("cc-agent", "Claude Code: agent detail",
              "Pick one agent (the list holds agents with data in the selected time range) and see its "
              "consumption, quality signals and errors for that range.", with_plugin=False, section="detail",
              single=("agent", "Agent", AGENTS_IN_RANGE, '/agent_name="([^"]+)"/'))
    A = '{agent_name=~"$agent"}'          # consumption, API errors, failed calls
    T = '{agent_type=~"$agent"}'          # subagent runs and durations (same value, other label)
    tok, cost = "claude_code:token_usage:delta5m", "claude_code:cost_usage:delta5m"
    runs, tuses = "claude_code:subagent_completions_by_agent:delta5m", "claude_code:subagent_tool_uses_by_agent:delta5m"
    inv = '{tool_name=~"Agent|Task",agent_name=~"$agent"}'
    bucket = "claude_code_subagent_duration_ms_bucket"

    b.row("Summary for the selected agent and time range")
    b.stat("Tokens", in_range(tok, A), description="All token types.")
    b.stat("Cost", in_range(cost, A), "currencyUSD", decimals=2)
    b.stat("USD per million tokens", f"{in_range(cost, A)} / ({in_range(tok, A)} / 1e6)", "currencyUSD", decimals=2)
    cache_in = f'claude_code:token_usage:delta5m{{agent_name=~"$agent",type=~"input|cacheRead|cacheCreation"}}'
    cache_rd = f'claude_code:token_usage:delta5m{{agent_name=~"$agent",type="cacheRead"}}'
    b.stat("Cache hit ratio", f"sum(sum_over_time({cache_rd}[$__range])) / sum(sum_over_time({cache_in}[$__range]))",
           "percentunit", decimals=0, description="cacheRead / (input + cacheRead + cacheCreation).")
    b.stat("Runs", in_range(runs, T), description="Subagent runs completed.")
    b.stat("Tool calls per run",
           f"{in_range(tuses, T)} / {in_range(runs, T)}", decimals=1,
           description="Average tool calls the agent made per run.")
    b.stat("Duration p95", f"histogram_quantile(0.95, sum by (le) (increase({bucket}{T}[$__range]))) / 1000", "s", decimals=1)
    b.stat("Average duration",
           f"sum(increase(claude_code_subagent_duration_ms_sum{T}[$__range])) / sum(increase(claude_code_subagent_duration_ms_count{T}[$__range])) / 1000",
           "s", decimals=1)
    b.stat("Invocation success rate",
           f'sum(sum_over_time(claude_code:tool_results:delta5m{{tool_name=~"Agent|Task",success="true",agent_name=~"$agent"}}[$__range])) / '
           f"{in_range('claude_code:tool_results:delta5m', inv)}", "percentunit", decimals=0,
           description="Share of Agent tool calls to this agent that succeeded.",
           thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.8}, {"color": "green", "value": 0.95}])
    b.stat("API errors", in_range("claude_code:api_errors:delta5m", A),
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 20}])
    b.stat("Failed calls", f'sum(sum_over_time(claude_code:tool_failures_by_component:delta5m{A}[$__range]))',
           description="Failed Agent tool calls to this agent.",
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 10}])

    b.row("Consumption")
    b.series("Tokens by type", [(f"sum by (type) ({tok}{A})", "{{type}}")], description="input / output / cacheRead / cacheCreation per 5-minute step.")
    b.series("Cost by model", [(f"sum by (model) ({cost}{A})", "{{model}}")], "currencyUSD")
    b.series("Tokens by skill active", [(f'sum by (skill_name) ({tok}{A})', "{{skill_name}}")],
             description="The skill that was active when the agent ran (agents inherit the active skill). Empty = none.")
    b.series("Tokens by host", [(f"sum by (host_name) ({tok}{A})", "{{host_name}}")])
    cum = "claude_code:token_usage_by_component:cumulative"
    b.series("Cumulative tokens since the start of the range",
             [(f"sum({cum}{A}) - (sum(last_over_time({cum}{A}[1h] @ start())) or vector(0))", "tokens")],
             bars=False, stack=False, description="Running total, zero at the left edge of the range.")
    ccum = "claude_code:cost_usage_by_component:cumulative"
    b.series("Cumulative cost since the start of the range",
             [(f"sum({ccum}{A}) - (sum(last_over_time({ccum}{A}[1h] @ start())) or vector(0))", "cost")],
             "currencyUSD", bars=False, stack=False)
    b.bars("Tokens by model and type, in range", f"sum by (model, type) (sum_over_time({tok}{A}[$__range]))", "{{model}} {{type}}")
    b.bars("Cost by skill active, in range", f"sum by (skill_name) (sum_over_time({cost}{A}[$__range]))", "{{skill_name}}", "currencyUSD")

    b.row("Quality signals")
    b.series("Runs", [(f"sum by (model) (claude_code:subagent_completions:delta5m{T})", "{{model}}")], description="Subagent runs completed per 5-minute step.")
    pct = lambda q: f"histogram_quantile({q}, sum by (le) (increase({bucket}{T}[15m]))) / 1000"
    b.series("Duration", [(pct(0.5), "p50"), (pct(0.95), "p95")], "s", bars=False, stack=False,
             description="Run time percentiles over a 15-minute window.")
    b.series("Tool calls per run", [(f"sum({tuses}{T}) / sum({runs}{T})", "tool calls per run")], bars=False, stack=False)
    b.series("Invocations by outcome", [(f"sum by (success) (claude_code:tool_results:delta5m{inv})", "success={{success}}")],
             description="Agent tool calls to this agent: did the invocation succeed?")
    b.bars("Runs by model, in range", f"sum by (model) (sum_over_time(claude_code:subagent_completions:delta5m{T}[$__range]))", "{{model}}")
    b.bars("Runs by host, in range", f"sum by (host_name) (sum_over_time(claude_code:subagent_completions:delta5m{T}[$__range]))", "{{host_name}}")

    b.row("Errors")
    b.text("Errors are attributed to this agent only when Claude Code names it: **API errors** issued by the agent and **failed "
           "Agent tool calls** to it. A failure *inside* the agent (a Bash error, say) cannot be attributed to it: those events carry no agent.")
    b.series("API errors by status", [(f"sum by (status_code, model) (claude_code:api_errors:delta5m{A})", "{{model}} {{status_code}}")])
    b.series("Failed calls by error type", [(f"sum by (error_type) (claude_code:tool_failures_by_component:delta5m{A})", "{{error_type}}")])
    b.bars("API errors by status, in range", f"sum by (model, status_code) (sum_over_time(claude_code:api_errors:delta5m{A}[$__range]))", "{{model}} {{status_code}}")
    b.bars("Failed calls by error type, in range", f"sum by (error_type) (sum_over_time(claude_code:tool_failures_by_component:delta5m{A}[$__range]))", "{{error_type}}")
    return b


def skill_detail() -> Board:
    b = Board("cc-skill", "Claude Code: skill detail",
              "Pick one skill (the list holds skills with data in the selected time range) and see its "
              "consumption, quality signals and errors for that range.", with_plugin=False, section="detail",
              single=("skill", "Skill", SKILLS_IN_RANGE, '/skill_name="([^"]+)"/'))
    S = '{skill_name=~"$skill"}'
    tok, cost = "claude_code:token_usage:delta5m", "claude_code:cost_usage:delta5m"
    act = "claude_code:skill_activations:delta5m"
    inv = '{tool_name="Skill",skill_name=~"$skill"}'

    b.row("Summary for the selected skill and time range")
    b.stat("Tokens", in_range(tok, S), description="All token types, including agents that ran while the skill was active.")
    b.stat("Cost", in_range(cost, S), "currencyUSD", decimals=2)
    b.stat("USD per million tokens", f"{in_range(cost, S)} / ({in_range(tok, S)} / 1e6)", "currencyUSD", decimals=2)
    cache_in = 'claude_code:token_usage:delta5m{skill_name=~"$skill",type=~"input|cacheRead|cacheCreation"}'
    cache_rd = 'claude_code:token_usage:delta5m{skill_name=~"$skill",type="cacheRead"}'
    b.stat("Cache hit ratio", f"sum(sum_over_time({cache_rd}[$__range])) / sum(sum_over_time({cache_in}[$__range]))",
           "percentunit", decimals=0, description="cacheRead / (input + cacheRead + cacheCreation).")
    b.stat("Activations", in_range(act, S), description="Times the skill was activated.")
    b.stat("Proactive share",
           f'{in_range(act, "{skill_name=~\"$skill\",invocation_trigger=\"claude-proactive\"}")} / {in_range(act, S)}',
           "percentunit", decimals=0,
           description="Share of activations Claude chose itself (the rest: you ran it with a slash command, or another skill called it). Low = hard to discover.")
    b.stat("Invocation success rate",
           f'sum(sum_over_time(claude_code:tool_results:delta5m{{tool_name="Skill",success="true",skill_name=~"$skill"}}[$__range])) / {in_range("claude_code:tool_results:delta5m", inv)}',
           "percentunit", decimals=0, description="Share of Skill tool calls for this skill that succeeded.",
           thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.8}, {"color": "green", "value": 0.95}])
    b.stat("API errors", in_range("claude_code:api_errors:delta5m", S),
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 20}])
    b.stat("Failed calls", f"sum(sum_over_time(claude_code:tool_failures_by_component:delta5m{S}[$__range]))",
           description="Failed Skill tool calls for this skill.",
           thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 10}])

    b.row("Consumption")
    b.series("Tokens by type", [(f"sum by (type) ({tok}{S})", "{{type}}")], description="input / output / cacheRead / cacheCreation per 5-minute step.")
    b.series("Cost by model", [(f"sum by (model) ({cost}{S})", "{{model}}")], "currencyUSD")
    b.series("Tokens by agent active", [(f"sum by (agent_name) ({tok}{S})", "{{agent_name}}")],
             description="Subagents that ran while this skill was active (agents inherit the active skill). Empty = the main session.")
    b.series("Tokens by host", [(f"sum by (host_name) ({tok}{S})", "{{host_name}}")])
    cum = "claude_code:token_usage_by_component:cumulative"
    b.series("Cumulative tokens since the start of the range",
             [(f"sum({cum}{S}) - (sum(last_over_time({cum}{S}[1h] @ start())) or vector(0))", "tokens")],
             bars=False, stack=False, description="Running total, zero at the left edge of the range.")
    ccum = "claude_code:cost_usage_by_component:cumulative"
    b.series("Cumulative cost since the start of the range",
             [(f"sum({ccum}{S}) - (sum(last_over_time({ccum}{S}[1h] @ start())) or vector(0))", "cost")],
             "currencyUSD", bars=False, stack=False)
    b.bars("Tokens by model and type, in range", f"sum by (model, type) (sum_over_time({tok}{S}[$__range]))", "{{model}} {{type}}")
    b.bars("Cost by agent active, in range", f"sum by (agent_name) (sum_over_time({cost}{S}[$__range]))", "{{agent_name}}", "currencyUSD")

    b.row("Quality signals")
    b.series("Activations by trigger", [(f"sum by (invocation_trigger) ({act}{S})", "{{invocation_trigger}}")],
             description="user-slash: you ran it; claude-proactive: Claude chose it; nested-skill: another skill called it.")
    b.series("Activations by source", [(f"sum by (skill_source) ({act}{S})", "{{skill_source}}")],
             description="Where the skill was loaded from (plugin, projectSettings, userSettings, bundled).")
    b.series("Invocations by outcome", [(f"sum by (success) (claude_code:tool_results:delta5m{inv})", "success={{success}}")],
             description="Skill tool calls for this skill: did the invocation succeed?")
    b.bars("Activations by trigger, in range", f"sum by (invocation_trigger) (sum_over_time({act}{S}[$__range]))", "{{invocation_trigger}}")
    b.bars("Activations by host, in range", f"sum by (host_name) (sum_over_time({act}{S}[$__range]))", "{{host_name}}")

    b.row("Errors")
    b.text("Errors are attributed to this skill only when Claude Code names it: **API errors** issued while the skill was active "
           "and **failed Skill tool calls** for it. A failure *inside* the skill (a Bash error, say) cannot be attributed to it: "
           "those events carry no skill.")
    b.series("API errors by status", [(f"sum by (status_code, model) (claude_code:api_errors:delta5m{S})", "{{model}} {{status_code}}")])
    b.series("Failed calls by error type", [(f"sum by (error_type) (claude_code:tool_failures_by_component:delta5m{S})", "{{error_type}}")])
    b.bars("API errors by status, in range", f"sum by (model, status_code) (sum_over_time(claude_code:api_errors:delta5m{S}[$__range]))", "{{model}} {{status_code}}")
    b.bars("Failed calls by error type, in range", f"sum by (error_type) (sum_over_time(claude_code:tool_failures_by_component:delta5m{S}[$__range]))", "{{error_type}}")
    return b


if __name__ == "__main__":
    for stale in OUT.rglob("*.json"):  # the output is fully generated: drop anything from an older layout
        stale.unlink()
    for board in (consumption(), quality(), errors(), agent_detail(), skill_detail()):
        board.write()
        print(f"wrote {OUT / SECTIONS[board.section] / (board.uid + '.json')} ({len(board.panels)} panels)")
