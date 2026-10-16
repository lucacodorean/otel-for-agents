#!/usr/bin/env bash
# Stop the pipeline (the counterpart of infra/start.sh):
#   1. Collector container   2. Grafana   3. dev Prometheus container
# Data volumes and ~/.claude/settings.json are kept unless you ask otherwise.
#
#   infra/stop.sh [--no-prometheus] [--no-grafana] [--disable] [--purge] [--dry-run]
#     --no-prometheus  leave the dev Prometheus running
#     --no-grafana     leave Grafana running
#     --disable        also run `disable`: remove the OTEL settings from ~/.claude/settings.json
#     --purge          also delete the Docker volumes (config, state, WAL, Grafana and dev Prometheus data)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
setup="$here/docker/otel_setup.sh"

prometheus=true grafana=true disable=false purge=false dry=false
for arg in "$@"; do
  case "$arg" in
    --no-prometheus) prometheus=false ;;
    --no-grafana)    grafana=false ;;
    --disable)       disable=true ;;
    --purge)         purge=true ;;
    --dry-run)       dry=true ;;
    -h|--help)       sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
dry_flag=(); $dry && dry_flag=(--dry-run)
purge_flag=(); $purge && purge_flag=(--purge)

command -v docker >/dev/null || { echo "error: docker is required" >&2; exit 2; }
[[ -f $repo/.env ]] || { echo "error: $repo/.env not found" >&2; exit 2; }

if $disable; then
  step "Claude Code settings"
  "$setup" disable "${dry_flag[@]}"
fi

step "Collector"
"$setup" collector uninstall "${purge_flag[@]}" "${dry_flag[@]}"

if $grafana; then
  step "Grafana"
  down=(down); $purge && down+=(--volumes)
  if $dry; then
    echo "[dry-run] would run: docker compose ${down[*]} (infra/grafana)"
  elif docker ps -a --format '{{.Names}}' | grep -qx otel-grafana; then
    # the compose file requires GRAFANA_ADMIN_PASSWORD to parse; "down" does not use it
    (cd "$repo/infra/grafana" && GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-unused}" docker compose --env-file ../../.env "${down[@]}")
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
    (cd "$repo/infra/prometheus" && docker compose --env-file ../../.env "${down[@]}")
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
