"""Helpers shared by the suites under tests/ (fixtures live in tests/conftest.py).

Docker-backed tests never bind-mount host paths: files are copied into containers with
`docker cp` and HTTP checks run from a curl container on the same network. That keeps
them working with a remote daemon too (GitLab docker:dind, Docker Desktop, OrbStack).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar

from otel_setup.config import Config

REPO = Path(__file__).resolve().parent.parent

# Minimal valid configuration; tests override single keys on top of it.
BASE_ENV = {
    "PROMETHEUS_REMOTE_WRITE_URL": "http://prometheus:9090/api/v1/write",
    "PROMETHEUS_USERNAME": "otel",
    "PROMETHEUS_PASSWORD": "s3cret-pass",
}


def compose_image(rel: str) -> str:
    """The pinned image of the first service in a compose file, so tests run what ships."""
    return re.search(r"^\s*image:\s*(\S+)", (REPO / rel).read_text(), re.M).group(1)


PROMETHEUS_IMAGE = compose_image("infra/prometheus/docker-compose.yml")
GRAFANA_IMAGE = compose_image("infra/grafana/docker-compose.yml")
COLLECTOR_IMAGE = f"otel/opentelemetry-collector-contrib:{Config.collector_version}"
CURL_IMAGE = "curlimages/curl:8.11.1"

T = TypeVar("T")


def wait_for(probe: Callable[[], T], timeout: float = 60, interval: float = 1, what: str = "condition") -> T:
    """Call `probe` until it returns something truthy; fail with its last result."""
    deadline = time.monotonic() + timeout
    last: T = probe()
    while not last:
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")
        time.sleep(interval)
        last = probe()
    return last


class Docker:
    """Creates containers and networks for one test module and removes them afterwards."""

    def __init__(self) -> None:
        self.tag = f"otel-test-{uuid.uuid4().hex[:8]}"
        self.containers: list[str] = []
        self.networks: list[str] = []

    def cli(self, *args: str, check: bool = True, input: str | None = None,
            timeout: float = 600) -> subprocess.CompletedProcess:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, input=input, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}{proc.stderr}")
        return proc

    def network(self) -> str:
        name = f"{self.tag}-net{len(self.networks)}"
        self.cli("network", "create", name)
        self.networks.append(name)
        return name

    def create(self, image: str, *args: str, entrypoint: str | None = None, env: dict[str, str] | None = None,
               network: str | None = None, aliases: tuple[str, ...] = (), hostname: str | None = None,
               tmpfs: tuple[str, ...] = (), files: dict[str, dict[str, str | Path]] | None = None) -> str:
        """`files` maps a container directory to {relative path: text or local Path}."""
        cmd = ["create", "--label", f"otel-test={self.tag}"]
        if entrypoint is not None:
            cmd += ["--entrypoint", entrypoint]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        if network:
            cmd += ["--network", network]
        for alias in aliases:
            cmd += ["--network-alias", alias]
        if hostname:
            cmd += ["--hostname", hostname]
        for mount in tmpfs:
            cmd += ["--tmpfs", mount]
        cid = self.cli(*cmd, image, *args).stdout.strip()
        self.containers.append(cid)
        for dest, content in (files or {}).items():
            self.copy_in(cid, dest, content)
        return cid

    def copy_in(self, cid: str, dest: str, files: dict[str, str | Path]) -> None:
        """Copy files into `dest`, world-readable: images run as non-root. `dest` is created if
        missing, but its parent must exist in the image (docker cp creates one level only)."""
        stage = Path(tempfile.mkdtemp(prefix="otel-test-"))
        try:
            for rel, content in files.items():
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, Path):
                    shutil.copyfile(content, target)
                else:
                    target.write_text(content)
            for path in [stage, *stage.rglob("*")]:
                path.chmod(0o755 if path.is_dir() else 0o644)
            self.cli("cp", f"{stage}/.", f"{cid}:{dest}")
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def start(self, cid: str) -> None:
        self.cli("start", cid)

    def run(self, image: str, *args: str, timeout: float = 300, **kwargs) -> tuple[int, str]:
        """Run a container to completion; return (exit code, combined output)."""
        cid = self.create(image, *args, **kwargs)
        self.start(cid)
        code = int(self.cli("wait", cid, timeout=timeout).stdout.strip())
        return code, self.logs(cid)

    def logs(self, cid: str) -> str:
        proc = self.cli("logs", cid, check=False)
        return proc.stdout + proc.stderr

    def running(self, cid: str) -> bool:
        return self.cli("inspect", "-f", "{{.State.Running}}", cid).stdout.strip() == "true"

    def http(self, network: str) -> "Http":
        cid = self.create(CURL_IMAGE, "infinity", entrypoint="sleep", network=network)
        self.start(cid)
        return Http(self, cid)

    def cleanup(self) -> None:
        if self.containers:
            self.cli("rm", "-f", "-v", *self.containers, check=False)
        for net in self.networks:
            self.cli("network", "rm", net, check=False)


class Http:
    """curl, run inside a container on the test network."""

    def __init__(self, docker: Docker, cid: str) -> None:
        self.docker, self.cid = docker, cid

    def request(self, url: str, *curl_args: str, data: str | None = None) -> tuple[int, str]:
        """Return (HTTP status, body); status 0 means no response (connection refused, ...)."""
        args = ["exec", "-i", self.cid, "curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "--max-time", "10",
                *curl_args]
        if data is not None:
            args += ["--data-binary", "@-"]
        proc = self.docker.cli(*args, url, check=False, input=data or "")
        body, _, code = proc.stdout.rpartition("\n")
        return (int(code) if code.isdigit() else 0), body or proc.stderr
