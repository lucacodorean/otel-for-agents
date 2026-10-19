"""The `otel_setup` command line, end to end against temporary files."""
from __future__ import annotations

import json

import bcrypt
import pytest
import yaml

from otel_setup import cli, paths, settings
from tests.support import BASE_ENV


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"

    def write(**overrides):
        path.write_text("".join(f"{k}={v}\n" for k, v in {**BASE_ENV, **overrides}.items()))
        return path

    write()
    return write


def test_enable_then_disable_restores_settings(env_file, capsys):
    path = paths.claude_settings_path()
    path.parent.mkdir(parents=True)
    original = {"theme": "dark", "env": {"FOO": "bar"}}
    path.write_text(json.dumps(original))

    assert cli.main(["--env-file", str(env_file()), "enable"]) == 0
    env = json.loads(path.read_text())["env"]
    assert env == {"FOO": "bar", **settings.managed_env(60000, quality_signals=True)}
    assert json.loads(paths.state_path().read_text())["claude_settings"]["managed"]
    assert "updated" in capsys.readouterr().out

    assert cli.main(["--env-file", str(env_file()), "enable"]) == 0
    assert "already up to date" in capsys.readouterr().out

    assert cli.main(["disable"]) == 0
    assert json.loads(path.read_text()) == original
    assert "claude_settings" not in json.loads(paths.state_path().read_text())
    backups = sorted(p.name for p in path.parent.glob("settings.json.otel-setup-*.bak"))
    assert len(backups) == 2, backups


def test_enable_dry_run_writes_nothing(env_file, capsys):
    assert cli.main(["--env-file", str(env_file()), "enable", "--dry-run"]) == 0
    assert "[dry-run] would" in capsys.readouterr().out
    assert not paths.claude_settings_path().exists()
    assert not paths.state_path().exists()


def test_disable_without_enable(capsys):
    assert cli.main(["disable"]) == 0
    assert "nothing to disable" in capsys.readouterr().out


def test_invalid_configuration_exits_2(env_file, capsys):
    assert cli.main(["--env-file", str(env_file(FILTER_MODE="nope")), "enable"]) == 2
    assert "error: Invalid configuration" in capsys.readouterr().err


def test_missing_env_file_exits_2(tmp_path, capsys):
    assert cli.main(["--env-file", str(tmp_path / "missing.env"), "status"]) == 2
    assert "not found" in capsys.readouterr().err


def test_collector_render(env_file):
    assert cli.main(["--env-file", str(env_file(QUALITY_SIGNALS="false")), "collector", "render"]) == 0
    doc = yaml.safe_load(paths.rendered_config_path().read_text())
    assert doc["service"]["pipelines"]["metrics"]["exporters"] == ["prometheus_remote_write"]


def test_collector_render_dry_run(env_file, capsys):
    assert cli.main(["--env-file", str(env_file()), "collector", "render", "--dry-run"]) == 0
    assert not paths.rendered_config_path().exists()


def test_marketplace_sync_and_list(env_file, tmp_path, capsys):
    mp = tmp_path / "mp"
    (mp / ".claude-plugin").mkdir(parents=True)
    (mp / ".claude-plugin" / "marketplace.json").write_text('{"plugins": [{"name": "p1", "source": "./p1"}]}')
    (mp / "p1" / "skills" / "s1").mkdir(parents=True)
    (mp / "p1" / "skills" / "s1" / "SKILL.md").write_text("skill\n")
    env = env_file(MARKETPLACES=f"acme={mp}")

    assert cli.main(["--env-file", str(env), "marketplace", "list"]) == 0
    assert "not synced" in capsys.readouterr().out

    assert cli.main(["--env-file", str(env), "marketplace", "sync"]) == 0
    assert "acme: 1 plugin(s), 1 skill(s), 0 agent(s)" in capsys.readouterr().out
    assert json.loads(paths.allowlist_path().read_text()) == {"acme": {"p1": {"skills": ["s1"], "agents": []}}}
    assert '\\"acme\\"' in paths.rendered_config_path().read_text()

    assert cli.main(["--env-file", str(env), "marketplace", "list"]) == 0
    out = capsys.readouterr().out
    assert "- p1" in out and "skills: s1" in out


def test_render_warns_about_unsynced_marketplaces(env_file, capsys):
    assert cli.main(["--env-file", str(env_file(MARKETPLACES="acme=./mp")), "collector", "render"]) == 0
    assert "not synced yet: acme" in capsys.readouterr().out


def test_old_allowlist_format_is_still_read(env_file):
    paths.allowlist_path().parent.mkdir(parents=True)
    paths.allowlist_path().write_text('{"acme": ["p1"]}')
    assert cli.main(["--env-file", str(env_file(MARKETPLACES="acme=./mp")), "collector", "render"]) == 0
    assert '\\"p1\\"' in paths.rendered_config_path().read_text()


def test_hash_password_from_env(env_file, capsys):
    assert cli.main(["--env-file", str(env_file()), "prometheus", "hash-password", "--from-env"]) == 0
    digest = capsys.readouterr().out.strip()
    assert bcrypt.checkpw(BASE_ENV["PROMETHEUS_PASSWORD"].encode(), digest.encode())


def test_status_reports_failures_without_a_running_stack(env_file, monkeypatch, capsys):
    monkeypatch.setattr("otel_setup.status.HEALTH_URL", "http://127.0.0.1:9/")
    env = env_file(PROMETHEUS_REMOTE_WRITE_URL="http://127.0.0.1:9/api/v1/write")
    assert cli.main(["--env-file", str(env), "status"]) == 1
    out = capsys.readouterr().out
    assert "[unset] CLAUDE_CODE_ENABLE_TELEMETRY=-" in out
    assert "unreachable" in out
    assert "Grafana: not configured" in out
