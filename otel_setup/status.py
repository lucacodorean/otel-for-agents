"""`status`: Claude settings, service state, health check and endpoint reachability."""
from __future__ import annotations

import base64
import os
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse

from otel_setup import paths
from otel_setup.config import Config
from otel_setup.service import load_allowlist
from otel_setup.settings import managed_env, read_settings
from otel_setup.state import load_json

HEALTH_URL = os.environ.get("COLLECTOR_HEALTH_URL", "http://127.0.0.1:13133/")


def health_check() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:  # noqa: S310
            return resp.status == 200, resp.read().decode()[:120]
    except (urllib.error.URLError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))


def _ssl_context(cfg: Config) -> ssl.SSLContext | None:
    if not cfg.tls_enabled:
        return None
    in_container = os.environ.get("OTEL_SETUP_IN_CONTAINER") == "1"
    ctx = ssl.create_default_context(cafile=cfg.container_ca_file if in_container else cfg.ca_file)
    if cfg.insecure_skip_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


GRAFANA_HEALTH_URL = os.environ.get("GRAFANA_HEALTH_URL", "http://grafana:3000/api/health")


def grafana_check() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(GRAFANA_HEALTH_URL, timeout=3) as resp:  # noqa: S310
            return resp.status == 200, "ok"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))


def endpoint_check(cfg: Config) -> tuple[bool, str]:
    """POST an empty body to the remote-write URL with the configured credentials.

    A healthy receiver answers 400 (empty body, but authenticated); 401/403 means
    the credentials are rejected; connection errors mean it is unreachable.
    """
    token = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
    req = urllib.request.Request(cfg.remote_write_url, data=b"", method="POST", headers={
        "Authorization": f"Basic {token}", "Content-Encoding": "snappy",
        "Content-Type": "application/x-protobuf", "X-Prometheus-Remote-Write-Version": "0.1.0"})
    try:
        urllib.request.urlopen(req, timeout=5, context=_ssl_context(cfg))  # noqa: S310
        return True, "reachable, credentials accepted"
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 204, 200):
            return True, f"reachable, credentials accepted (HTTP {exc.code} to empty probe)"
        if exc.code in (401, 403):
            return False, f"reachable, but credentials rejected (HTTP {exc.code})"
        return False, f"reachable, unexpected HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable: {getattr(exc, 'reason', exc)}"


def report(cfg: Config) -> bool:
    ok = True
    host = urlparse(cfg.remote_write_url).netloc

    print("Claude Code settings (" + str(paths.claude_settings_path()) + ")")
    env = read_settings(paths.claude_settings_path()).get("env", {})
    for key, want in managed_env(cfg.export_interval_ms, cfg.quality_signals).items():
        have = env.get(key)
        mark = "ok  " if have == want else ("diff" if have is not None else "unset")
        ok &= have == want
        print(f"  [{mark}] {key}={have if have is not None else '-'}")

    healthy, detail = health_check()
    print(f"\nCollector health ({HEALTH_URL}): {'ok' if healthy else 'FAILED (is the container up? infra/docker/otel_setup.sh collector install)'} {detail}")
    ok &= healthy

    reachable, detail = endpoint_check(cfg)
    print(f"Remote write endpoint ({host}): {detail}")
    ok &= reachable

    if cfg.grafana_admin_password:  # Grafana is only expected when it has been configured
        up, detail = grafana_check()
        print(f"Grafana ({GRAFANA_HEALTH_URL}): {'ok' if up else 'not running (infra/start.sh starts it)'}"
              + ("" if up else f" [{detail}]"))
    else:
        print("Grafana: not configured (set GRAFANA_ADMIN_PASSWORD in .env to use it)")

    print("\nMarketplaces (filter mode: " + cfg.filter_mode + ")")
    if not cfg.marketplaces:
        print("  none configured: no filtering or labelling, everything passes through")
    allowlist = load_allowlist()
    for mp in cfg.marketplaces:
        plugins = allowlist.get(mp.name)
        summary = "not synced" if plugins is None else (
            f"{len(plugins)} plugin(s), {sum(len(c['skills']) for c in plugins.values())} skill(s), "
            f"{sum(len(c['agents']) for c in plugins.values())} agent(s)")
        print(f"  {mp.name}: {mp.source}{'@' + mp.ref if mp.ref else ''} - {summary}")
    if cfg.extra_plugins:
        print("  extra allowlist: " + ", ".join(cfg.extra_plugins))
    return ok
