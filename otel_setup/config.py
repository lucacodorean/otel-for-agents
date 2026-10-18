"""Loading and validation of the .env configuration."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from otel_setup.errors import ConfigError
from otel_setup.paths import REPO_ROOT

DEFAULT_WAL_DIR = "/var/lib/otelcol/wal"  # a path inside the Collector container (a Docker volume)
CONTAINER_CA_FILE = "/etc/otelcol/ca.pem"  # where PROMETHEUS_CA_FILE is mounted in the containers
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DURATION_RE = r"0|\d+(ms|s|m|h|d|w|y)"
BOOLS = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}


@dataclass(frozen=True)
class Marketplace:
    name: str
    source: str
    ref: str | None = None

    @property
    def is_local(self) -> bool:
        return is_local_source(self.source)


@dataclass(frozen=True)
class Config:
    remote_write_url: str
    username: str
    password: str
    tls_enabled: bool = False
    ca_file: str | None = None
    insecure_skip_verify: bool = False
    grafana_url: str | None = None
    marketplaces: tuple[Marketplace, ...] = ()
    extra_plugins: tuple[str, ...] = ()
    filter_mode: str = "drop"
    collector_version: str = "0.162.0"
    wal_dir: Path = Path(DEFAULT_WAL_DIR)
    export_interval_ms: int = 60000
    timezone: str = "UTC"
    quality_signals: bool = True
    grafana_port: int = 3000
    grafana_admin_password: str | None = None
    dev_port: int = 9090
    dev_retention: str = "2d"
    dev_retention_size: str | None = None
    dev_out_of_order_window: str = "2d"
    extra: dict = field(default_factory=dict)

    @property
    def container_ca_file(self) -> str | None:
        """Where the containers see PROMETHEUS_CA_FILE (compose bind-mounts it here)."""
        return CONTAINER_CA_FILE if self.ca_file else None


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments and single/double quoted values."""
    out: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f".env line {n}: expected KEY=VALUE, got {raw!r}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f".env line {n}: invalid variable name {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)  # trailing comment
        out[key] = value
    return out


def is_local_source(source: str) -> bool:
    return source.startswith(("/", "./", "../", "~")) or source in (".", "..")


def parse_marketplaces(raw: str) -> list[Marketplace]:
    """Parse `name=source[@ref],name2=source2` (see README for the ref rule)."""
    result: list[Marketplace] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        if "=" not in entry:
            raise ConfigError(f"MARKETPLACES entry {entry!r}: expected name=source[@ref]")
        name, rest = entry.split("=", 1)
        name, rest = name.strip(), rest.strip()
        if not NAME_RE.match(name):
            raise ConfigError(f"MARKETPLACES entry {entry!r}: invalid name {name!r}")
        if not rest:
            raise ConfigError(f"MARKETPLACES entry {name!r}: empty source")
        source, ref = rest, None
        if "@" in rest and not is_local_source(rest):
            head, _, tail = rest.rpartition("@")
            # A ref has no ':' or '/', so scp-style `git@host:path` is not split.
            if tail and ":" not in tail and "/" not in tail and head:
                source, ref = head, tail
        result.append(Marketplace(name=name, source=source, ref=ref))
    names = [m.name for m in result]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"MARKETPLACES: duplicate marketplace name(s): {', '.join(dupes)}")
    return result


def _bool(env: dict[str, str], key: str, default: bool, errors: list[str]) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw not in BOOLS:
        errors.append(f"{key}: expected true or false, got {raw!r}")
        return default
    return BOOLS[raw]


def _int(env: dict[str, str], key: str, default: int, minimum: int, errors: list[str]) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{key}: expected an integer, got {raw!r}")
        return default
    if value < minimum:
        errors.append(f"{key}: must be >= {minimum}, got {value}")
    return value


def build_config(env: dict[str, str]) -> Config:
    errors: list[str] = []
    get = lambda k: env.get(k, "").strip()  # noqa: E731

    url = get("PROMETHEUS_REMOTE_WRITE_URL")
    tls = _bool(env, "PROMETHEUS_TLS_ENABLED", False, errors)
    if not url:
        errors.append("PROMETHEUS_REMOTE_WRITE_URL: required")
    else:
        parsed = urlparse(url)
        if any(c in url for c in "'\"$\\") or any(c.isspace() for c in url):
            errors.append("PROMETHEUS_REMOTE_WRITE_URL: must not contain quotes, $, backslashes or whitespace")
        elif parsed.scheme not in ("http", "https") or not parsed.hostname:
            errors.append(f"PROMETHEUS_REMOTE_WRITE_URL: not a valid http(s) URL: {url!r}")
        elif os.environ.get("OTEL_SETUP_IN_CONTAINER") == "1" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            errors.append(
                "PROMETHEUS_REMOTE_WRITE_URL: the Collector runs in a container, where "
                f"{parsed.hostname} is the container itself. Use a name on the shared Docker network "
                "(dev: http://prometheus:9090/api/v1/write) or the real server's hostname")
        elif tls and parsed.scheme != "https":
            errors.append("PROMETHEUS_REMOTE_WRITE_URL: must start with https:// when PROMETHEUS_TLS_ENABLED=true")
        elif not tls and parsed.scheme == "https":
            errors.append("PROMETHEUS_TLS_ENABLED must be true when PROMETHEUS_REMOTE_WRITE_URL is https://")

    username = get("PROMETHEUS_USERNAME")
    if not username:
        errors.append("PROMETHEUS_USERNAME: required (remote write always uses basic auth)")
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        errors.append("PROMETHEUS_USERNAME: only letters, digits, '_', '.' and '-' are allowed")

    password = env.get("PROMETHEUS_PASSWORD", "")
    if not password:
        errors.append("PROMETHEUS_PASSWORD: required (remote write always uses basic auth)")
    elif any(c in password for c in "'\"$\\") or any(c.isspace() or c == "\0" for c in password):
        errors.append("PROMETHEUS_PASSWORD: must not contain quotes, $, backslashes or whitespace "
                      "(docker compose reads .env and would interpret them)")
    elif len(password.encode()) > 72:
        errors.append("PROMETHEUS_PASSWORD: bcrypt supports at most 72 bytes")

    ca_file = get("PROMETHEUS_CA_FILE") or None
    if ca_file:
        ca_file = str(Path(ca_file).expanduser())
        if not tls:
            errors.append("PROMETHEUS_CA_FILE: only meaningful when PROMETHEUS_TLS_ENABLED=true")
        elif os.environ.get("OTEL_SETUP_IN_CONTAINER") != "1" and not Path(ca_file).is_file():
            errors.append(f"PROMETHEUS_CA_FILE: file not found: {ca_file}")
    insecure = _bool(env, "PROMETHEUS_INSECURE_SKIP_VERIFY", False, errors)
    if insecure and not tls:
        errors.append("PROMETHEUS_INSECURE_SKIP_VERIFY: only meaningful when PROMETHEUS_TLS_ENABLED=true")

    marketplaces: list[Marketplace] = []
    try:
        marketplaces = parse_marketplaces(env.get("MARKETPLACES", ""))
    except ConfigError as exc:
        errors.append(str(exc))

    extras = tuple(p.strip() for p in env.get("EXTRA_PLUGIN_ALLOWLIST", "").split(",") if p.strip())

    mode = (get("FILTER_MODE") or "drop").lower()
    if mode not in ("drop", "label"):
        errors.append(f"FILTER_MODE: expected 'drop' or 'label', got {mode!r}")

    version = get("COLLECTOR_VERSION") or "0.162.0"
    if not VERSION_RE.match(version):
        errors.append(f"COLLECTOR_VERSION: expected X.Y.Z, got {version!r}")

    wal_raw = get("COLLECTOR_WAL_DIR") or DEFAULT_WAL_DIR
    wal_dir = Path(wal_raw)
    if not wal_dir.is_absolute() or wal_raw.startswith("~"):
        errors.append(f"COLLECTOR_WAL_DIR: must be an absolute path inside the Collector container "
                      f"(default {DEFAULT_WAL_DIR}; it lives on the collector-wal Docker volume), got {wal_raw!r}")

    tz = get("COLLECTOR_TIMEZONE") or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"COLLECTOR_TIMEZONE: unknown IANA timezone {tz!r} (e.g. UTC, Europe/Bucharest, Etc/GMT-3)")

    quality = _bool(env, "QUALITY_SIGNALS", True, errors)

    grafana_port = _int(env, "GRAFANA_PORT", 3000, 1, errors)
    if grafana_port > 65535:
        errors.append("GRAFANA_PORT: must be <= 65535")
    grafana_password = env.get("GRAFANA_ADMIN_PASSWORD", "") or None
    if grafana_password and (any(c in grafana_password for c in "'\"$\\") or any(c.isspace() for c in grafana_password)):
        errors.append("GRAFANA_ADMIN_PASSWORD: must not contain quotes, $, backslashes or whitespace")
    for key in ("GRAFANA_URL", "GRAFANA_PROMETHEUS_URL"):
        value = get(key)
        if value and urlparse(value).scheme not in ("http", "https"):
            errors.append(f"{key}: not a valid http(s) URL: {value!r}")

    interval = _int(env, "METRICS_EXPORT_INTERVAL_MS", 60000, 1000, errors)
    port = _int(env, "DEV_PROMETHEUS_PORT", 9090, 1, errors)
    if port > 65535:
        errors.append("DEV_PROMETHEUS_PORT: must be <= 65535")
    retention = get("DEV_PROMETHEUS_RETENTION") or "2d"
    if not re.fullmatch(DURATION_RE, retention):
        errors.append(f"DEV_PROMETHEUS_RETENTION: expected e.g. 2d or 12h, got {retention!r}")
    retention_size = get("DEV_PROMETHEUS_RETENTION_SIZE") or None
    if retention_size and not re.fullmatch(r"\d+(B|KB|MB|GB|TB|PB|EB)", retention_size):
        errors.append(f"DEV_PROMETHEUS_RETENTION_SIZE: expected e.g. 512MB or 20GB, got {retention_size!r}")
    no_time_limit = bool(re.fullmatch(r"0+[a-z]*", retention))
    if no_time_limit and not retention_size:
        # Prometheus silently falls back to 15d when neither limit is set.
        errors.append("DEV_PROMETHEUS_RETENTION: 0 (no time limit) needs DEV_PROMETHEUS_RETENTION_SIZE")
    ooo_window = get("DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW") or (None if no_time_limit else retention)
    if ooo_window is None:
        errors.append("DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW: required when DEV_PROMETHEUS_RETENTION is 0")
    elif not re.fullmatch(DURATION_RE, ooo_window):
        errors.append(f"DEV_PROMETHEUS_OUT_OF_ORDER_WINDOW: expected e.g. 2d or 12h, got {ooo_window!r}")

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    return Config(
        remote_write_url=url, username=username, password=password, tls_enabled=tls,
        ca_file=ca_file, insecure_skip_verify=insecure, grafana_url=get("GRAFANA_URL") or None,
        marketplaces=tuple(marketplaces), extra_plugins=extras, filter_mode=mode,
        collector_version=version, wal_dir=wal_dir, export_interval_ms=interval, timezone=tz, quality_signals=quality, grafana_port=grafana_port,
        grafana_admin_password=grafana_password,
        dev_port=port, dev_retention=retention, dev_retention_size=retention_size,
        dev_out_of_order_window=ooo_window or "0s",
    )


def load_config(env_file: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    """Load `.env` (default: <repo>/.env). Real environment variables override it."""
    path = env_file or REPO_ROOT / ".env"
    if not path.is_file():
        raise ConfigError(f"{path} not found. Copy .env.example to .env and fill it in.")
    values = parse_dotenv(path.read_text())
    values.update({k: v for k, v in (os.environ if environ is None else environ).items() if k in values})
    return build_config(values)
