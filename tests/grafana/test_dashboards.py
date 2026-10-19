"""Grafana: the generated dashboards, provisioning, and a real Grafana loading them."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

from tests.support import BASE_ENV, GRAFANA_IMAGE, REPO, wait_for

GRAFANA_DIR = REPO / "infra" / "grafana"
DASHBOARDS = GRAFANA_DIR / "dashboards"
RULES = REPO / "infra" / "prometheus" / "recording-rules.yml"
RULE_REF = re.compile(r"claude_code:[a-z0-9_]+:[a-z0-9_]+")
# Claude Code's own per-session series (session_id label). Event-derived histograms such as
# claude_code_subagent_duration_ms_bucket carry no session and may be queried directly.
PER_SESSION = re.compile(
    r"\bclaude_code_(token_usage|cost_usage|session|active_time|lines_of_code|commit|pull_request|code_edit)\w*")


def load_generator():
    if sys.version_info < (3, 12):  # the script uses PEP 701 f-strings (backslashes inside {...})
        pytest.skip("infra/grafana/generate_dashboards.py needs Python >= 3.12")
    spec = importlib.util.spec_from_file_location("generate_dashboards", GRAFANA_DIR / "generate_dashboards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def committed() -> dict[str, dict]:
    return {str(p.relative_to(DASHBOARDS)): json.loads(p.read_text()) for p in sorted(DASHBOARDS.rglob("*.json"))}


def all_panels(board: dict) -> list[dict]:
    out = []
    for panel in board["panels"]:
        out.append(panel)
        out.extend(panel.get("panels", []))
    return out


def expressions(board: dict) -> list[str]:
    exprs = [t["expr"] for p in all_panels(board) for t in p.get("targets", [])]
    exprs += [v["query"]["query"] if isinstance(v["query"], dict) else v["query"]
              for v in board["templating"]["list"] if v.get("type") == "query"]
    return exprs


def test_committed_dashboards_match_the_generator(tmp_path, monkeypatch):
    gen = load_generator()
    monkeypatch.setattr(gen, "OUT", tmp_path)
    for board in (gen.consumption(), gen.quality(), gen.errors(), gen.agent_detail(), gen.skill_detail()):
        board.write()
    generated = {str(p.relative_to(tmp_path)): p.read_text() for p in sorted(tmp_path.rglob("*.json"))}
    on_disk = {str(p.relative_to(DASHBOARDS)): p.read_text() for p in sorted(DASHBOARDS.rglob("*.json"))}
    assert generated.keys() == on_disk.keys()
    stale = [name for name in generated if generated[name] != on_disk[name]]
    assert not stale, f"re-run `python3 infra/grafana/generate_dashboards.py` and commit: {stale}"


def test_folders_match_sections():
    gen = load_generator()
    assert {p.name for p in DASHBOARDS.iterdir() if p.is_dir()} == set(gen.SECTIONS.values())


def test_dashboard_structure():
    boards = committed()
    uids = [b["uid"] for b in boards.values()]
    assert len(uids) == len(set(uids)), "dashboard uids must be unique"
    for name, board in boards.items():
        assert len(board["uid"]) <= 40, name
        ids = [p["id"] for p in all_panels(board)]
        assert len(ids) == len(set(ids)), f"{name}: duplicate panel ids"
        for panel in all_panels(board):
            pos = panel["gridPos"]
            assert pos["x"] >= 0 and pos["x"] + pos["w"] <= 24, f"{name}: {panel['title']} overflows the grid"
            for target in panel.get("targets", []):
                assert target["datasource"]["uid"] == "prometheus", f"{name}: {panel['title']}"


def test_dashboards_only_query_existing_recording_rules():
    defined = {r["record"] for g in yaml.safe_load(RULES.read_text())["groups"] for r in g["rules"]}
    for name, board in committed().items():
        for expr in expressions(board):
            missing = set(RULE_REF.findall(expr)) - defined
            assert not missing, f"{name}: {expr!r} uses undefined rules {missing}"
            assert not PER_SESSION.findall(expr), f"{name}: {expr!r} queries raw per-session series"


def test_provisioned_datasource_uid_matches_dashboards():
    doc = yaml.safe_load((GRAFANA_DIR / "provisioning" / "datasources" / "prometheus.yml").read_text())
    [ds] = doc["datasources"]
    assert ds["uid"] == load_generator().DS["uid"]


def test_provisioning_variables_are_set_by_compose():
    compose = yaml.safe_load((GRAFANA_DIR / "docker-compose.yml").read_text())
    environment = compose["services"]["grafana"]["environment"]
    for path in (GRAFANA_DIR / "provisioning").rglob("*.yml"):
        for var in re.findall(r"\$\{(\w+)\}", path.read_text()):
            assert var in environment, f"{path.name} reads ${{{var}}}, which compose does not set"


def test_compose_variables_are_documented_in_env_example():
    example = (REPO / ".env.example").read_text()
    for var in set(re.findall(r"\$\{(\w+)", (GRAFANA_DIR / "docker-compose.yml").read_text())):
        assert re.search(rf"^{var}=", example, re.M), f"{var} is not in .env.example"


# --- a real Grafana ------------------------------------------------------------------------

ADMIN = ("admin", "ci-admin-pass")


@pytest.fixture(scope="module")
def grafana(docker):
    dashboards = {str(p.relative_to(DASHBOARDS)): p for p in DASHBOARDS.rglob("*.json")}
    provisioning = {str(p.relative_to(GRAFANA_DIR / "provisioning")): p
                    for p in (GRAFANA_DIR / "provisioning").rglob("*.yml")}
    net = docker.network()
    cid = docker.create(GRAFANA_IMAGE, network=net, aliases=("grafana",), env={
        "GF_SECURITY_ADMIN_USER": ADMIN[0], "GF_SECURITY_ADMIN_PASSWORD": ADMIN[1],
        "GF_ANALYTICS_REPORTING_ENABLED": "false", "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "PROMETHEUS_DS_URL": "http://prometheus:9090", "PROMETHEUS_DS_USER": BASE_ENV["PROMETHEUS_USERNAME"],
        "PROMETHEUS_DS_PASSWORD": BASE_ENV["PROMETHEUS_PASSWORD"],
    }, files={"/etc/grafana/provisioning": provisioning, "/var/lib/grafana/dashboards": dashboards})
    docker.start(cid)
    http = docker.http(net)
    wait_for(lambda: http.request("http://grafana:3000/api/health")[0] == 200, 120,
             what=f"Grafana healthy\n{docker.logs(cid)[-4000:]}")

    def api(path: str):
        status, body = http.request(f"http://grafana:3000{path}", "-u", ":".join(ADMIN))
        assert status == 200, f"{path}: HTTP {status} {body}"
        return json.loads(body)
    return api, cid, docker


def test_grafana_provisions_every_dashboard_into_its_folder(grafana):
    api, cid, docker = grafana
    expected = {b["uid"]: Path(name).parent.name for name, b in committed().items()}

    def found():
        hits = api("/api/search?type=dash-db")
        return {h["uid"]: h.get("folderTitle") for h in hits} if len(hits) >= len(expected) else None
    assert wait_for(found, 60, 2, what=f"dashboards provisioned\n{docker.logs(cid)[-4000:]}") == expected


def test_grafana_provisions_the_datasource(grafana):
    api, _, _ = grafana
    ds = api("/api/datasources/uid/prometheus")
    assert (ds["type"], ds["url"], ds["basicAuth"], ds["basicAuthUser"]) == (
        "prometheus", "http://prometheus:9090", True, BASE_ENV["PROMETHEUS_USERNAME"])
    assert ds["readOnly"] is True


def test_grafana_dashboards_load(grafana):
    api, _, _ = grafana
    for uid in (b["uid"] for b in committed().values()):
        board = api(f"/api/dashboards/uid/{uid}")
        assert board["meta"]["provisioned"] is True
        assert board["dashboard"]["panels"], uid
