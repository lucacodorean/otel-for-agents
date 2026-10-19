"""recording-rules.yml: static checks, then promtool (check + unit tests) in the pinned image."""
from __future__ import annotations

import re

import yaml

from otel_setup.config import build_config
from otel_setup.render import render_prometheus_files
from tests.support import BASE_ENV, PROMETHEUS_IMAGE, REPO

RULES = REPO / "infra" / "prometheus" / "recording-rules.yml"
RULE_TESTS = REPO / "tests" / "prometheus" / "recording-rules.test.yml"
NAME_RE = re.compile(r"^claude_code:[a-z0-9_]+:[a-z0-9_]+$")


def groups() -> list[dict]:
    return yaml.safe_load(RULES.read_text())["groups"]


def test_rules_follow_the_naming_scheme():
    for group in groups():
        assert group["name"].startswith("claude_code_")
        for rule in group["rules"]:
            assert set(rule) <= {"record", "expr", "labels"}, f"{rule}: only recording rules belong here"
            assert NAME_RE.match(rule["record"]), rule["record"]


def test_delta_groups_run_at_their_window():
    """Delta windows must not overlap: a group of *:delta5m rules runs every 5m."""
    for group in groups():
        windows = {r["record"].rsplit(":", 1)[1] for r in group["rules"]}
        if "delta5m" in windows:
            assert group["interval"] == "5m", group["name"]


def test_rule_tests_cover_every_group():
    tested = set(re.findall(r"claude_code:[a-z0-9_]+:[a-z0-9_]+", RULE_TESTS.read_text()))
    for group in groups():
        assert tested & {r["record"] for r in group["rules"]}, f"no unit test touches group {group['name']}"


def _promtool(docker, *args: str) -> tuple[int, str]:
    files = {"infra/prometheus/recording-rules.yml": RULES, "tests/prometheus/recording-rules.test.yml": RULE_TESTS}
    return docker.run(PROMETHEUS_IMAGE, *args, entrypoint="promtool", files={"/work": files})


def test_promtool_check_rules(docker):
    code, out = _promtool(docker, "check", "rules", "/work/infra/prometheus/recording-rules.yml")
    assert code == 0, out


def test_promtool_rule_unit_tests(docker):
    code, out = _promtool(docker, "test", "rules", "/work/tests/prometheus/recording-rules.test.yml")
    assert code == 0, out


def test_promtool_check_rendered_config(docker):
    cfg = build_config({**BASE_ENV, "DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW": "6h"})
    files = {**render_prometheus_files(cfg, "$2b$10$" + "x" * 53), "recording-rules.yml": RULES}
    code, out = docker.run(PROMETHEUS_IMAGE, "check", "config", "/etc/prometheus/prometheus.yml",
                           entrypoint="promtool", files={"/etc/prometheus": files})
    assert code == 0, out
    code, out = docker.run(PROMETHEUS_IMAGE, "check", "web-config", "/etc/prometheus/web-config.yml",
                           entrypoint="promtool", files={"/etc/prometheus": files})
    assert code == 0, out
