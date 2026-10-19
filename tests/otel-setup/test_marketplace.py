"""Marketplace catalogs and the allowlist (otel_setup.marketplace)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from otel_setup import marketplace
from otel_setup.config import Marketplace
from otel_setup.errors import MarketplaceError


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def make_marketplace(root: Path) -> Path:
    """p1: in-repo plugin with skills and agents; p2: external source; p3: escapes the repo."""
    write(root / ".claude-plugin" / "marketplace.json", json.dumps({"name": "acme", "plugins": [
        {"name": "p1", "source": "./plugins/p1"},
        {"name": "p2", "source": {"source": "github", "repo": "acme/p2"}},
        {"name": "p3", "source": "./../outside"},
    ]}))
    p1 = root / "plugins" / "p1"
    write(p1 / "skills" / "first" / "SKILL.md", "---\nname: skill-one\ndescription: x\n---\nbody\n")
    write(p1 / "skills" / "second" / "SKILL.md", "no frontmatter\n")
    write(p1 / "skills" / "not-a-skill" / "README.md", "ignored\n")
    write(p1 / "agents" / "rev.md", "---\ndescription: x\nname: 'reviewer'\n---\n")
    write(p1 / "agents" / "plain.md", "plain agent\n")
    write(root.parent / "outside" / "skills" / "evil" / "SKILL.md", "---\nname: evil\n---\n")
    return root


EXPECTED = {
    "p1": {"skills": ["second", "skill-one"], "agents": ["plain", "reviewer"]},
    "p2": {"skills": [], "agents": []},
    "p3": {"skills": [], "agents": []},
}


def test_parse_catalog():
    assert marketplace.parse_catalog('{"plugins": [{"name": " a "}, {"name": "b"}]}', "x") == ["a", "b"]


@pytest.mark.parametrize("text, message", [
    ("{", "invalid JSON"),
    ("[]", "expected a 'plugins' array"),
    ('{"plugins": {}}', "expected a 'plugins' array"),
    ('{"plugins": [{"source": "./x"}]}', r"plugins\[0\] has no 'name'"),
    ('{"plugins": [{"name": "  "}]}', r"plugins\[0\] has no 'name'"),
    ('{"plugins": [{"name": "a"}, {"name": "a"}]}', "duplicate plugin name"),
])
def test_parse_catalog_errors(text, message):
    with pytest.raises(MarketplaceError, match=message):
        marketplace.parse_catalog(text, "x")


def test_read_local_marketplace(tmp_path):
    root = make_marketplace(tmp_path / "mp")
    assert marketplace.read_marketplace(Marketplace("acme", str(root)), tmp_path / "cache") == EXPECTED


def test_local_marketplace_errors(tmp_path):
    with pytest.raises(MarketplaceError, match="local path not found"):
        marketplace.read_marketplace(Marketplace("acme", str(tmp_path / "missing")), tmp_path)
    (tmp_path / "empty").mkdir()
    with pytest.raises(MarketplaceError, match="marketplace.json not found"):
        marketplace.read_marketplace(Marketplace("acme", str(tmp_path / "empty")), tmp_path)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_read_git_marketplace_at_a_ref(tmp_path):
    repo = make_marketplace(tmp_path / "repo")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
           "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "v1"], check=True)
    subprocess.run([*git, "tag", "v1"], check=True)
    (repo / "plugins" / "p1" / "agents" / "plain.md").unlink()
    subprocess.run([*git, "commit", "-qam", "v2"], check=True)

    cache = tmp_path / "cache"
    at_tag = marketplace.read_marketplace(Marketplace("acme", repo.as_uri(), "v1"), cache)
    assert at_tag == EXPECTED
    head = marketplace.read_marketplace(Marketplace("acme", repo.as_uri()), cache)
    assert head["p1"]["agents"] == ["reviewer"]
    assert (cache / "acme" / ".claude-plugin" / "marketplace.json").is_file()
    assert [p.name for p in cache.iterdir()] == ["acme"], "temporary checkouts are removed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_failure_is_reported(tmp_path):
    with pytest.raises(MarketplaceError, match="git fetch failed"):
        marketplace.read_marketplace(Marketplace("acme", (tmp_path / "nope").as_uri()), tmp_path / "cache")
    assert list((tmp_path / "cache").iterdir()) == []


def test_build_allowlist_sorts_and_dedupes():
    result = marketplace.build_allowlist({"a": {"p": {"skills": ["z", "y", "z"]}}})
    assert result == {"a": {"p": {"skills": ["y", "z"], "agents": []}}}


@pytest.mark.parametrize("catalogs, message", [
    ({"a": {"p": {}}, "b": {"p": {}}}, "plugin 'p' is defined by both"),
    ({"a": {"p": {"skills": ["s"]}, "q": {"skills": ["s"]}}}, "skill 's' is defined by both"),
    ({"a": {"p": {"agents": ["r"]}}, "b": {"q": {"agents": ["r"]}}}, "agent 'r' is defined by both"),
])
def test_build_allowlist_rejects_ambiguous_names(catalogs, message):
    with pytest.raises(MarketplaceError, match=message):
        marketplace.build_allowlist(catalogs)


def test_sync(make_config, tmp_path):
    root = make_marketplace(tmp_path / "mp")
    cfg = make_config(MARKETPLACES=f"acme={root}")
    assert marketplace.sync(cfg, tmp_path / "cache") == {"acme": EXPECTED}
