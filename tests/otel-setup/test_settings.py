"""Merging the OTEL env block into ~/.claude/settings.json (otel_setup.settings)."""
from __future__ import annotations

import json
import stat
from datetime import datetime

import pytest

from otel_setup import settings
from otel_setup.errors import SetupError


def desired(quality: bool = True) -> dict[str, str]:
    return settings.managed_env(60000, quality)


def test_managed_env():
    env = settings.managed_env(5000)
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == settings.OTLP_ENDPOINT
    assert env["OTEL_METRIC_EXPORT_INTERVAL"] == "5000"
    assert "OTEL_LOGS_EXPORTER" not in env
    assert settings.managed_env(5000, quality_signals=True)["OTEL_LOGS_EXPORTER"] == "otlp"
    # Prompt and response logging must never be switched on by this tool.
    assert not set(settings.FORBIDDEN_UNSET) & set(settings.managed_env(5000, True))


def test_merge_into_empty_document():
    doc: dict = {}
    plan = settings.merge(doc, desired(), {})
    assert doc == {}, "merge must not mutate its input"
    assert plan.document == {"env": desired()}
    assert sorted(plan.added) == sorted(desired())
    assert plan.state["created_env"] is True
    assert plan.state["written"] == desired()
    assert set(plan.state["previous"].values()) == {None}


def test_merge_keeps_unrelated_settings_and_records_replaced_values():
    doc = {"theme": "dark", "env": {"FOO": "bar", "OTEL_METRICS_EXPORTER": "console"}}
    plan = settings.merge(doc, desired(), {})
    assert plan.document["theme"] == "dark"
    assert plan.document["env"]["FOO"] == "bar"
    assert plan.document["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
    assert plan.changed == ["OTEL_METRICS_EXPORTER"]
    assert plan.state["previous"]["OTEL_METRICS_EXPORTER"] == "console"
    assert plan.state["created_env"] is False


def test_merge_is_idempotent():
    first = settings.merge({}, desired(), {})
    second = settings.merge(first.document, desired(), first.state)
    assert not second.modified
    assert second.document is first.document


@pytest.mark.parametrize("doc", [
    {},
    {"theme": "dark"},
    {"env": {}},
    {"env": {"FOO": "bar"}, "model": "opus"},
    {"env": {"OTEL_METRICS_EXPORTER": "console", "OTEL_METRIC_EXPORT_INTERVAL": "1000"}},
])
def test_unmerge_restores_the_original_document(doc):
    plan = settings.merge(doc, desired(), {})
    undo = settings.unmerge(plan.document, plan.state)
    assert undo.document == doc


def test_unmerge_leaves_keys_the_user_changed_since_enable():
    plan = settings.merge({}, desired(), {})
    plan.document["env"]["OTEL_METRIC_EXPORT_INTERVAL"] = "1234"
    undo = settings.unmerge(plan.document, plan.state)
    assert undo.kept == ["OTEL_METRIC_EXPORT_INTERVAL"]
    assert undo.document == {"env": {"OTEL_METRIC_EXPORT_INTERVAL": "1234"}}


def test_turning_quality_signals_off_removes_the_logs_exporter():
    on = settings.merge({}, desired(quality=True), {})
    off = settings.merge(on.document, desired(quality=False), on.state)
    assert off.removed == ["OTEL_LOGS_EXPORTER"]
    assert "OTEL_LOGS_EXPORTER" not in off.document["env"]
    assert "OTEL_LOGS_EXPORTER" not in off.state["managed"]


def test_turning_quality_signals_off_restores_a_previous_logs_exporter():
    on = settings.merge({"env": {"OTEL_LOGS_EXPORTER": "console"}}, desired(quality=True), {})
    off = settings.merge(on.document, desired(quality=False), on.state)
    assert off.restored == ["OTEL_LOGS_EXPORTER"]
    assert off.document["env"]["OTEL_LOGS_EXPORTER"] == "console"


def test_merge_warns_about_prompt_logging():
    plan = settings.merge({"env": {"OTEL_LOG_USER_PROMPTS": "1"}}, desired(), {})
    assert any("OTEL_LOG_USER_PROMPTS" in w for w in plan.warnings)


def test_env_must_be_an_object():
    with pytest.raises(SetupError, match="'env' must be an object"):
        settings.merge({"env": ["x"]}, desired(), {})


def test_read_settings(tmp_path):
    path = tmp_path / "settings.json"
    assert settings.read_settings(path) == {}
    path.write_text("  \n")
    assert settings.read_settings(path) == {}
    path.write_text('{"a": 1}')
    assert settings.read_settings(path) == {"a": 1}
    path.write_text("{not json")
    with pytest.raises(SetupError, match="not valid JSON"):
        settings.read_settings(path)
    path.write_text("[]")
    with pytest.raises(SetupError, match="JSON object"):
        settings.read_settings(path)


def test_backup_names_are_unique(tmp_path):
    path = tmp_path / "settings.json"
    assert settings.backup(path) is None
    path.write_text("{}")
    now = datetime(2026, 1, 2, 3, 4, 5)
    first, second = settings.backup(path, now), settings.backup(path, now)
    assert first.name == "settings.json.otel-setup-20260102T030405.bak"
    assert second.name == "settings.json.otel-setup-20260102T030405-1.bak"
    assert first.read_text() == second.read_text() == "{}"


def test_write_settings_is_atomic_and_keeps_the_mode(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    settings.write_settings(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o640)
    settings.write_settings(path, {"b": 2})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"], "no temp files left behind"
