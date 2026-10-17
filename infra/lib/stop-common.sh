#!/usr/bin/env bash
# The OS-independent part of infra/stop.sh (see its header for the steps and options).
# Run through infra/stop.sh, which picks infra/linux/stop.sh or infra/macos/stop.sh.
set -euo pipefail

infra="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(dirname "$infra")"
setup="$infra/docker/otel_setup.sh"
source "$infra/lib/platform.sh"

prometheus=true grafana=true disable=false purge=false dry=false
for arg in "$@"; do
  case "$arg" in
    --no-prometheus) prometheus=false ;;
    --no-grafana)    grafana=false ;;
    --disable)       disable=true ;;
    --purge)         purge=true ;;
    --dry-run)       dry=true ;;
    -h|--help)       otel_usage "$infra/stop.sh"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
dry_flag=(); $dry && dry_flag=(--dry-run)
purge_flag=(); $purge && purge_flag=(--purge)

otel_require_docker_cli
[[ -f $repo/.env ]] || { echo "error: $repo/.env not found" >&2; exit 2; }

if $disable; then
  step "Claude Code settings"
  "$setup" disable ${dry_flag[@]+"${dry_flag[@]}"}
fi

step "Collector"
"$setup" collector uninstall ${purge_flag[@]+"${purge_flag[@]}"} ${dry_flag[@]+"${dry_flag[@]}"}

if $grafana; then
  step "Grafana"
  down=(down); $purge && down+=(--volumes)
  if $dry; then
    echo "[dry-run] would run: docker compose ${down[*]} (infra/grafana)"
  elif docker ps -a --format '{{.Names}}' | grep -qx otel-grafana; then
    # the compose file requires GRAFANA_ADMIN_PASSWORD to parse; "down" does not use it
    (cd "$infra/grafana" && GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-unused}" docker compose --env-file ../../.env "${down[@]}")
  else
    echo "not running"
  fi
fi

if $prometheus; then
  step "dev Prometheus"
  down=(down); $purge && down+=(--volumes)
  if $dry; then
    echo "[dry-run] would run: docker compose ${down[*]} (infra/prometheus)"
  elif docker ps -a --format '{{.Names}}' | grep -qx otel-dev-prometheus; then
    (cd "$infra/prometheus" && docker compose --env-file ../../.env "${down[@]}")
  else
    echo "not running"
  fi
fi

if $purge && ! $dry; then
  step "shared network"
  docker network rm otel-net >/dev/null 2>&1 && echo "removed otel-net" || echo "kept otel-net (still in use)"
fi

if $dry; then
  step "dry run finished; nothing was changed"
else
  step "stopped"
  $disable || echo "Claude Code settings are unchanged: sessions keep exporting to a Collector that is not running."
  $disable || echo "Run with --disable (or: infra/docker/otel_setup.sh disable) to remove them."
fi
