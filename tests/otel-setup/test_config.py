"""Loading and validation of .env (otel_setup.config)."""
from __future__ import annotations

from pathlib import Path

import pytest

from otel_setup.config import (DEFAULT_WAL_DIR, Marketplace, build_config, load_config, parse_dotenv,
                               parse_marketplaces)
from otel_setup.errors import ConfigError
from tests.support import BASE_ENV, REPO


# --- parse_dotenv --------------------------------------------------------------------------

def test_parse_dotenv_handles_comments_quotes_and_export():
    text = "\n".join([
        "# a comment",
        "",
        "A=1",
        'export B="x # not a comment"',
        "C=value   # trailing comment",
        "D='single'",
        "E=",
        "F=a=b",
    ])
    assert parse_dotenv(text) == {"A": "1", "B": "x # not a comment", "C": "value", "D": "single", "E": "",
                                  "F": "a=b"}


@pytest.mark.parametrize("line, message", [
    ("NOEQUALS", "expected KEY=VALUE"),
    ("1BAD=x", "invalid variable name"),
    ("BAD-KEY=x", "invalid variable name"),
])
def test_parse_dotenv_rejects_malformed_lines(line, message):
    with pytest.raises(ConfigError, match=message):
        parse_dotenv(f"OK=1\n{line}")


# --- parse_marketplaces --------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("", []),
    ("acme=https://git.example.com/acme/mp.git",
     [Marketplace("acme", "https://git.example.com/acme/mp.git")]),
    ("acme=https://git.example.com/acme/mp.git@main",
     [Marketplace("acme", "https://git.example.com/acme/mp.git", "main")]),
    # scp-style URLs: the '@' of git@host is not a ref separator ...
    ("gh=git@github.com:org/mp.git", [Marketplace("gh", "git@github.com:org/mp.git")]),
    # ... but a trailing @ref still is.
    ("gh=git@github.com:org/mp.git@v1.2.0", [Marketplace("gh", "git@github.com:org/mp.git", "v1.2.0")]),
    # Local paths are never split.
    ("local=/srv/mp@weird", [Marketplace("local", "/srv/mp@weird")]),
    (" a = ./mp , b=../other ,", [Marketplace("a", "./mp"), Marketplace("b", "../other")]),
])
def test_parse_marketplaces(raw, expected):
    assert parse_marketplaces(raw) == expected


def test_marketplace_is_local():
    assert Marketplace("a", "~/mp").is_local
    assert Marketplace("a", ".").is_local
    assert not Marketplace("a", "https://x/y.git").is_local


@pytest.mark.parametrize("raw, message", [
    ("acme", "expected name=source"),
    ("-acme=./mp", "invalid name"),
    ("acme=", "empty source"),
    ("a=./x,a=./y", "duplicate marketplace name"),
])
def test_parse_marketplaces_errors(raw, message):
    with pytest.raises(ConfigError, match=message):
        parse_marketplaces(raw)


# --- build_config --------------------------------------------------------------------------

def test_defaults(make_config):
    cfg = make_config()
    assert cfg.remote_write_url == BASE_ENV["PROMETHEUS_REMOTE_WRITE_URL"]
    assert (cfg.filter_mode, cfg.quality_signals, cfg.tls_enabled) == ("drop", True, False)
    assert cfg.collector_version == "0.162.0"
    assert cfg.wal_dir == Path(DEFAULT_WAL_DIR)
    assert (cfg.export_interval_ms, cfg.timezone) == (60000, "UTC")
    assert (cfg.dev_port, cfg.dev_retention, cfg.dev_retention_size) == (9090, "2d", None)
    assert cfg.dev_out_of_order_window == "2d"
    assert cfg.marketplaces == () and cfg.extra_plugins == ()
    assert cfg.container_ca_file is None


def test_shipped_env_example_is_valid():
    cfg = build_config(parse_dotenv((REPO / ".env.example").read_text()))
    assert cfg.filter_mode == "drop"


def test_values_are_parsed(make_config):
    cfg = make_config(FILTER_MODE="LABEL", QUALITY_SIGNALS="no", EXTRA_PLUGIN_ALLOWLIST=" p1, ,p2 ",
                      MARKETPLACES="acme=./mp", METRICS_EXPORT_INTERVAL_MS="5000", COLLECTOR_TIMEZONE="Etc/GMT-3",
                      DEV_PROMETHEUS_RETENTION="30d", DEV_PROMETHEUS_RETENTION_SIZE="20GB")
    assert cfg.filter_mode == "label"
    assert cfg.quality_signals is False
    assert cfg.extra_plugins == ("p1", "p2")
    assert cfg.marketplaces == (Marketplace("acme", "./mp"),)
    assert cfg.export_interval_ms == 5000
    assert cfg.timezone == "Etc/GMT-3"
    # The out-of-order window follows the retention unless set explicitly.
    assert (cfg.dev_retention, cfg.dev_retention_size, cfg.dev_out_of_order_window) == ("30d", "20GB", "30d")


def test_out_of_order_window_can_differ_from_retention(make_config):
    assert make_config(DEV_PROMETHEUS_RETENTION="30d",
                       DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW="1h").dev_out_of_order_window == "1h"


def test_retention_zero_needs_size_and_window(make_config):
    cfg = make_config(DEV_PROMETHEUS_RETENTION="0", DEV_PROMETHEUS_RETENTION_SIZE="512MB",
                      DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW="2d")
    assert cfg.dev_out_of_order_window == "2d"
    with pytest.raises(ConfigError, match="DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW: required"):
        make_config(DEV_PROMETHEUS_RETENTION="0", DEV_PROMETHEUS_RETENTION_SIZE="512MB")


def test_tls_with_ca_file(make_config, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy")
    cfg = make_config(PROMETHEUS_REMOTE_WRITE_URL="https://prom.example.com/api/v1/write",
                      PROMETHEUS_TLS_ENABLED="true", PROMETHEUS_CA_FILE=str(ca))
    assert cfg.tls_enabled and cfg.ca_file == str(ca)
    assert cfg.container_ca_file == "/etc/otelcol/ca.pem"


HTTPS = {"PROMETHEUS_REMOTE_WRITE_URL": "https://prom.example.com/api/v1/write", "PROMETHEUS_TLS_ENABLED": "true"}


@pytest.mark.parametrize("env, message", [
    ({"PROMETHEUS_REMOTE_WRITE_URL": ""}, "PROMETHEUS_REMOTE_WRITE_URL: required"),
    ({"PROMETHEUS_REMOTE_WRITE_URL": "ftp://prom/write"}, "not a valid http"),
    ({"PROMETHEUS_REMOTE_WRITE_URL": "http://prom/$x"}, "must not contain quotes"),
    ({"PROMETHEUS_REMOTE_WRITE_URL": "https://prom/write"}, "PROMETHEUS_TLS_ENABLED must be true"),
    ({"PROMETHEUS_TLS_ENABLED": "true"}, "must start with https://"),
    ({"PROMETHEUS_TLS_ENABLED": "sometimes"}, "PROMETHEUS_TLS_ENABLED: expected true or false"),
    ({"PROMETHEUS_USERNAME": ""}, "PROMETHEUS_USERNAME: required"),
    ({"PROMETHEUS_USERNAME": "bad user"}, "only letters"),
    ({"PROMETHEUS_PASSWORD": ""}, "PROMETHEUS_PASSWORD: required"),
    ({"PROMETHEUS_PASSWORD": "pa$$"}, "must not contain quotes"),
    ({"PROMETHEUS_PASSWORD": "a" * 73}, "at most 72 bytes"),
    ({**HTTPS, "PROMETHEUS_CA_FILE": "/does/not/exist.pem"}, "file not found"),
    ({"PROMETHEUS_CA_FILE": "/etc/hosts"}, "only meaningful when PROMETHEUS_TLS_ENABLED"),
    ({"PROMETHEUS_INSECURE_SKIP_VERIFY": "true"}, "only meaningful when PROMETHEUS_TLS_ENABLED"),
    ({"MARKETPLACES": "broken"}, "MARKETPLACES entry"),
    ({"FILTER_MODE": "keep"}, "FILTER_MODE: expected"),
    ({"COLLECTOR_VERSION": "latest"}, "expected X.Y.Z"),
    ({"COLLECTOR_WAL_DIR": "relative/wal"}, "must be an absolute path"),
    ({"COLLECTOR_WAL_DIR": "~/wal"}, "must be an absolute path"),
    ({"COLLECTOR_TIMEZONE": "Mars/Olympus_Mons"}, "unknown IANA timezone"),
    ({"QUALITY_SIGNALS": "maybe"}, "QUALITY_SIGNALS: expected true or false"),
    ({"GRAFANA_PORT": "70000"}, "GRAFANA_PORT: must be <= 65535"),
    ({"GRAFANA_PORT": "http"}, "GRAFANA_PORT: expected an integer"),
    ({"GRAFANA_ADMIN_PASSWORD": "has space"}, "GRAFANA_ADMIN_PASSWORD: must not contain"),
    ({"GRAFANA_URL": "grafana:3000"}, "GRAFANA_URL: not a valid http"),
    ({"GRAFANA_PROMETHEUS_URL": "prometheus:9090"}, "GRAFANA_PROMETHEUS_URL: not a valid http"),
    ({"METRICS_EXPORT_INTERVAL_MS": "500"}, "must be >= 1000"),
    ({"DEV_PROMETHEUS_PORT": "0"}, "DEV_PROMETHEUS_PORT: must be >= 1"),
    ({"DEV_PROMETHEUS_PORT": "65536"}, "DEV_PROMETHEUS_PORT: must be <= 65535"),
    ({"DEV_PROMETHEUS_RETENTION": "2 days"}, "DEV_PROMETHEUS_RETENTION: expected"),
    ({"DEV_PROMETHEUS_RETENTION": "0"}, "needs DEV_PROMETHEUS_RETENTION_SIZE"),
    ({"DEV_PROMETHEUS_RETENTION_SIZE": "20G"}, "DEV_PROMETHEUS_RETENTION_SIZE: expected"),
    ({"DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW": "soon"}, "DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW: expected"),
])
def test_invalid_values(make_config, env, message):
    with pytest.raises(ConfigError, match=message):
        make_config(**env)


def test_all_errors_are_reported_at_once(make_config):
    with pytest.raises(ConfigError) as exc:
        make_config(PROMETHEUS_USERNAME="", PROMETHEUS_PASSWORD="", FILTER_MODE="x")
    text = str(exc.value)
    assert "PROMETHEUS_USERNAME" in text and "PROMETHEUS_PASSWORD" in text and "FILTER_MODE" in text


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_url_is_rejected_inside_the_container(make_config, monkeypatch, host):
    make_config(PROMETHEUS_REMOTE_WRITE_URL=f"http://{host}:9090/api/v1/write")  # fine on the host
    monkeypatch.setenv("OTEL_SETUP_IN_CONTAINER", "1")
    with pytest.raises(ConfigError, match="the container itself"):
        make_config(PROMETHEUS_REMOTE_WRITE_URL=f"http://{host}:9090/api/v1/write")


# --- load_config ---------------------------------------------------------------------------

def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / ".env")


def test_environment_overrides_only_keys_present_in_the_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in BASE_ENV.items()))
    cfg = load_config(env_file, environ={"PROMETHEUS_PASSWORD": "from-environ", "FILTER_MODE": "label"})
    assert cfg.password == "from-environ"
    assert cfg.filter_mode == "drop"
