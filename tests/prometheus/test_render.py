"""Rendering of the dev Prometheus files (otel_setup.prometheus, `prometheus render-config`)."""
from __future__ import annotations

import re
import stat

import bcrypt
import pytest
import yaml

from otel_setup import cli, prometheus
from otel_setup.errors import SetupError
from otel_setup.render import render_prometheus_files
from otel_setup.service import Out
from tests.support import BASE_ENV, REPO

PROM_DIR = REPO / "infra" / "prometheus"


@pytest.fixture
def prom_dir(tmp_path, monkeypatch):
    """Write rendered files to tmp_path instead of infra/prometheus (templates still come from the repo)."""
    out = tmp_path / "prometheus"
    out.mkdir()
    monkeypatch.setattr(prometheus, "PROM_DIR", out)
    return out


def test_render_prometheus_files(make_config):
    cfg = make_config(DEV_PROMETHEUS_RETENTION="30d", DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW="6h")
    files = render_prometheus_files(cfg, "$2b$10$hash")
    assert set(files) == {"web-config.yml", "prometheus.yml", "scrape-password"}
    for name, text in files.items():
        assert not re.search(r"__[A-Z_]+__", text), name
    assert yaml.safe_load(files["web-config.yml"]) == {"basic_auth_users": {"otel": "$2b$10$hash"}}
    prom = yaml.safe_load(files["prometheus.yml"])
    assert prom["storage"]["tsdb"]["out_of_order_time_window"] == "6h"
    assert prom["rule_files"] == ["recording-rules.yml"]
    [job] = prom["scrape_configs"]
    assert job["basic_auth"] == {"username": "otel", "password_file": "/etc/prometheus/scrape-password"}
    assert files["scrape-password"] == BASE_ENV["PROMETHEUS_PASSWORD"]


def test_hash_password():
    digest = prometheus.hash_password("pw")
    assert bcrypt.checkpw(b"pw", digest.encode())
    for bad in ("", "a" * 73):
        with pytest.raises(SetupError, match="1-72 bytes"):
            prometheus.hash_password(bad)


def test_render_config_writes_readable_files(make_config, prom_dir):
    prometheus.render_config(make_config(), Out(dry_run=False))
    assert sorted(p.name for p in prom_dir.iterdir()) == ["prometheus.yml", "scrape-password", "web-config.yml"]
    for path in prom_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o644, f"{path.name}: the container user must read it"


def test_render_config_keeps_a_matching_hash(make_config, prom_dir):
    prometheus.render_config(make_config(), Out(dry_run=False))
    first = (prom_dir / "web-config.yml").read_text()
    prometheus.render_config(make_config(), Out(dry_run=False))
    assert (prom_dir / "web-config.yml").read_text() == first, "re-rendering must be idempotent"
    prometheus.render_config(make_config(PROMETHEUS_PASSWORD="another-one"), Out(dry_run=False))
    changed = yaml.safe_load((prom_dir / "web-config.yml").read_text())["basic_auth_users"]["otel"]
    assert bcrypt.checkpw(b"another-one", changed.encode())


def test_render_config_dry_run(make_config, prom_dir, capsys):
    prometheus.render_config(make_config(), Out(dry_run=True))
    assert list(prom_dir.iterdir()) == []
    assert capsys.readouterr().out.count("[dry-run] would write") == 3


def test_cli_render_config(tmp_path, prom_dir):
    env = tmp_path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in BASE_ENV.items()))
    assert cli.main(["--env-file", str(env), "prometheus", "render-config"]) == 0
    assert (prom_dir / "scrape-password").read_text() == BASE_ENV["PROMETHEUS_PASSWORD"]


def test_rendered_files_are_git_ignored():
    ignored = (REPO / ".gitignore").read_text().splitlines()
    for name in ("web-config.yml", "prometheus.yml", "scrape-password"):
        assert f"infra/prometheus/{name}" in ignored, f"{name} contains secrets"


def test_compose_variables_are_documented_in_env_example():
    example = (REPO / ".env.example").read_text()
    used = set(re.findall(r"\$\{(\w+)", (PROM_DIR / "docker-compose.yml").read_text()))
    assert used, "no variables found"
    for var in used:
        assert re.search(rf"^{var}=", example, re.M), f"{var} is used by the compose file but not in .env.example"
