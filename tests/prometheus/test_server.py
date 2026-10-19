"""The dev Prometheus, started as infra/prometheus/docker-compose.yml starts it, with rendered files."""
from __future__ import annotations

import json
import re

import pytest
import yaml

from otel_setup.config import build_config
from otel_setup.prometheus import hash_password
from otel_setup.render import render_prometheus_files
from tests.support import BASE_ENV, PROMETHEUS_IMAGE, REPO, wait_for

PROM_DIR = REPO / "infra" / "prometheus"
URL = "http://prometheus:9090"


def compose_flags(env: dict[str, str]) -> list[str]:
    """The `command:` of the compose service, with ${VAR:-default} resolved like compose would."""
    service = yaml.safe_load((PROM_DIR / "docker-compose.yml").read_text())["services"]["prometheus"]

    def resolve(match: re.Match) -> str:
        return env.get(match.group(1)) or match.group(2)
    return [re.sub(r"\$\{(\w+):-([^}]*)\}", resolve, arg) for arg in service["command"]]


@pytest.fixture(scope="module")
def server(docker):
    env = {**BASE_ENV, "DEV_PROMETHEUS_RETENTION": "3d", "DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW": "6h"}
    cfg = build_config(env)
    files = {**render_prometheus_files(cfg, hash_password(cfg.password)),
             "recording-rules.yml": PROM_DIR / "recording-rules.yml"}
    net = docker.network()
    cid = docker.create(PROMETHEUS_IMAGE, *compose_flags(env), network=net, aliases=("prometheus",),
                        files={"/etc/prometheus": files})
    docker.start(cid)
    http = docker.http(net)
    auth = ("-u", f"{cfg.username}:{cfg.password}")
    wait_for(lambda: http.request(f"{URL}/-/ready", *auth)[0] == 200, 90, what=f"ready\n{docker.logs(cid)}")
    return http, auth


def test_compose_flags_resolve():
    flags = compose_flags({"DEV_PROMETHEUS_RETENTION": "3d"})
    assert "--storage.tsdb.retention.time=3d" in flags
    assert "--storage.tsdb.retention.size=0B" in flags
    assert "--web.enable-remote-write-receiver" in flags


@pytest.mark.parametrize("user", [None, "otel:wrong", "someone:s3cret-pass"])
def test_every_endpoint_requires_basic_auth(server, user):
    http, _ = server
    for path in ("/-/ready", "/metrics", "/api/v1/query?query=up"):
        status, _ = http.request(f"{URL}{path}", *(("-u", user) if user else ()))
        assert status == 401, path


def test_rules_are_loaded(server):
    http, auth = server
    status, body = http.request(f"{URL}/api/v1/rules", *auth)
    assert status == 200
    loaded = {g["name"]: {r["name"] for r in g["rules"]} for g in json.loads(body)["data"]["groups"]}
    expected = yaml.safe_load((PROM_DIR / "recording-rules.yml").read_text())["groups"]
    assert loaded == {g["name"]: {r["record"] for r in g["rules"]} for g in expected}


def test_retention_and_out_of_order_window(server):
    http, auth = server
    _, body = http.request(f"{URL}/api/v1/status/flags", *auth)
    assert json.loads(body)["data"]["storage.tsdb.retention.time"] == "3d"
    _, body = http.request(f"{URL}/api/v1/status/config", *auth)
    config = yaml.safe_load(json.loads(body)["data"]["yaml"])
    assert config["storage"]["tsdb"]["out_of_order_time_window"] == "6h"


def test_self_scrape_authenticates(server):
    """The scrape uses scrape-password: if it did not match web-config, up would be 0."""
    http, auth = server

    def up():
        _, body = http.request(f"{URL}/api/v1/query", *auth, "-G", "--data-urlencode", 'query=up{job="prometheus"}')
        return json.loads(body)["data"]["result"]
    [series] = wait_for(up, 60, 2, what="first self-scrape")
    assert series["value"][1] == "1"


def test_remote_write_probe(server):
    """`otel_setup status` relies on this: an authenticated empty write answers 400, not 401/404."""
    http, auth = server
    headers = ("-H", "Content-Encoding: snappy", "-H", "Content-Type: application/x-protobuf",
               "-H", "X-Prometheus-Remote-Write-Version: 0.1.0")
    status, _ = http.request(f"{URL}/api/v1/write", *auth, *headers, "-X", "POST", data="")
    assert status == 400
    status, _ = http.request(f"{URL}/api/v1/write", *headers, "-X", "POST", data="")
    assert status == 401
