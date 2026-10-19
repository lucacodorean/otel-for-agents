"""Fixtures for all suites. Tests that use the `docker` fixture are marked `docker`:

    pytest -m "not docker"        # fast, no Docker needed
    REQUIRE_DOCKER=1 pytest -m docker   # fail (instead of skip) when Docker is missing
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from otel_setup.config import build_config
from tests.support import BASE_ENV, Docker


def pytest_collection_modifyitems(items):
    for item in items:
        if "docker" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.docker)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every path the tool writes to into tmp_path: never the real ~/.claude."""
    home = tmp_path / "home"
    monkeypatch.setenv("OTEL_SETUP_HOME", str(home / "data"))
    monkeypatch.setenv("OTEL_SETUP_CACHE", str(home / "cache"))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(home / ".claude" / "settings.json"))
    monkeypatch.delenv("OTEL_SETUP_IN_CONTAINER", raising=False)
    return home


@pytest.fixture
def make_config():
    """make_config(FILTER_MODE="label", ...) -> Config, on top of a minimal valid .env."""
    def make(**env):
        return build_config({**BASE_ENV, **{k: str(v) for k, v in env.items()}})
    return make


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def docker():
    if not _docker_available():
        if os.environ.get("REQUIRE_DOCKER") == "1":
            pytest.fail("REQUIRE_DOCKER=1 but no Docker daemon is reachable")
        pytest.skip("no Docker daemon reachable")
    d = Docker()
    yield d
    d.cleanup()
