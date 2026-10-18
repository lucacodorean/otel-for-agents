#!/usr/bin/env bash
# The OS-independent part of infra/start.sh (see its header for the steps and options).
# Run through infra/start.sh, which picks infra/linux/start.sh or infra/macos/start.sh;
# those check the Docker setup of their OS and then exec this script.
set -euo pipefail

infra="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(dirname "$infra")"
setup="$infra/docker/otel_setup.sh"
source "$infra/lib/platform.sh"

prometheus=true grafana=true sync=true enable=true dry=false
for arg in "$@"; do
  case "$arg" in
    --no-prometheus) prometheus=false ;;
    --no-grafana)    grafana=false ;;
    --no-sync)       sync=false ;;
    --no-enable)     enable=false ;;
    --dry-run)       dry=true ;;
    -h|--help)       otel_usage "$infra/start.sh"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
dry_flag=(); $dry && dry_flag=(--dry-run)

otel_require_docker_cli
[[ -f $repo/.env ]] || { echo "error: $repo/.env not found. Copy .env.example to .env and fill it in." >&2; exit 2; }

env_value() { { grep -E "^$1=" "$repo/.env" || true; } | tail -n1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]\$//"; }

# The dev Prometheus is reachable from the Collector as http://prometheus:...
url="$(env_value PROMETHEUS_REMOTE_WRITE_URL)"
marketplaces="$(env_value MARKETPLACES)"

if $prometheus && [[ $url == http://prometheus:* || $url == http://prometheus/* ]]; then
  step "dev Prometheus"
  "$setup" prometheus render-config ${dry_flag[@]+"${dry_flag[@]}"}
  if $dry; then
    echo "[dry-run] would start infra/prometheus (docker compose up -d)"
  else
    docker network inspect otel-net >/dev/null 2>&1 || docker network create otel-net >/dev/null
    started="$(docker inspect -f '{{.State.StartedAt}}' otel-dev-prometheus 2>/dev/null || true)"
    (cd "$infra/prometheus" && docker compose --env-file ../../.env up -d)
    # A changed prometheus.yml alone does not recreate the container, so reload it. Only
    # when compose left it running: a SIGHUP before Prometheus is ready terminates it.
    if [[ -n $started && $started == "$(docker inspect -f '{{.State.StartedAt}}' otel-dev-prometheus)" ]]; then
      docker kill -s HUP otel-dev-prometheus >/dev/null
    fi
  fi
else
  if $prometheus; then reason="the endpoint is $url, not the dev Prometheus"; else reason="--no-prometheus"; fi
  step "dev Prometheus: skipped ($reason)"
fi

grafana_password="$(env_value GRAFANA_ADMIN_PASSWORD)"
if $grafana && [[ -n $grafana_password ]]; then
  step "Grafana"
  if $dry; then
    echo "[dry-run] would start infra/grafana (docker compose up -d)"
  else
    docker network inspect otel-net >/dev/null 2>&1 || docker network create otel-net >/dev/null
    (cd "$infra/grafana" && docker compose --env-file ../../.env up -d)
  fi
elif $grafana; then
  step "Grafana: skipped (set GRAFANA_ADMIN_PASSWORD in .env to start it)"
else
  step "Grafana: skipped (--no-grafana)"
fi

if $sync && [[ -n $marketplaces ]]; then
  step "marketplace sync"
  "$setup" marketplace sync ${dry_flag[@]+"${dry_flag[@]}"}
elif $sync; then
  step "marketplace sync: skipped (MARKETPLACES is empty, everything passes through)"
fi

step "Collector"
"$setup" collector install ${dry_flag[@]+"${dry_flag[@]}"}

if $enable; then
  step "Claude Code settings"
  "$setup" enable ${dry_flag[@]+"${dry_flag[@]}"}
fi

if $dry; then
  step "dry run finished; nothing was changed"
  exit 0
fi

step "status"
status=1
for _ in 1 2 3 4 5 6 7 8 9 10; do   # the Collector and Prometheus need a few seconds to be ready
  if "$setup" status; then status=0; break; fi
  sleep 3
done

# --- endpoints ---------------------------------------------------------------
dev_port="$(env_value DEV_PROMETHEUS_PORT)"; dev_port="${dev_port:-9090}"
if [[ $url == http://prometheus:* || $url == http://prometheus/* ]]; then
  base="http://127.0.0.1:$dev_port"; kind="dev Prometheus, as seen from the host"
  user_note="log in with PROMETHEUS_USERNAME / PROMETHEUS_PASSWORD from .env (basic auth)"
else
  base="$(printf '%s' "$url" | sed -E 's#^(https?://[^/]+).*#\1#')"; kind="configured Prometheus"
  user_note="basic auth"
fi
printf '\nPrometheus (%s): %s   [%s]\n' "$kind" "$base" "$user_note"
cat <<ENDPOINTS
  UI (graph / table)   $base/graph
  Query API            $base/api/v1/query?query=<promql>
  Series API           $base/api/v1/series
  Rules                $base/rules
  Targets              $base/targets
  Metrics              $base/metrics
  Ready / healthy      $base/-/ready   $base/-/healthy
  Remote write         $url   (as the Collector sees it)
ENDPOINTS
if $grafana && [[ -n $grafana_password ]]; then
  grafana_port="$(env_value GRAFANA_PORT)"
  grafana_url="$(env_value GRAFANA_URL)"; grafana_url="${grafana_url:-http://127.0.0.1:${grafana_port:-3000}}"
  grafana_user="$(env_value GRAFANA_ADMIN_USER)"
  cat <<GRAFANA

Grafana: $grafana_url   [user: ${grafana_user:-admin}, password: GRAFANA_ADMIN_PASSWORD from .env]
  Consumption          $grafana_url/d/cc-consumption
  Quality signals      $grafana_url/d/cc-quality
  Errors               $grafana_url/d/cc-errors
GRAFANA
fi
cat <<ENDPOINTS

Collector (host, loopback only)
  OTLP HTTP / gRPC     http://127.0.0.1:4318   127.0.0.1:4317
  Health check         http://127.0.0.1:13133
  Own metrics          http://127.0.0.1:8888/metrics
ENDPOINTS

if [[ $status -eq 0 ]]; then
  printf '\nUp. Start a NEW Claude Code session (running ones do not re-read settings),\n'
  printf 'use a marketplace skill, wait ~2 minutes, then query Prometheus.\n'
fi
exit $status
