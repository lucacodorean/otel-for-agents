#!/usr/bin/env bash
# Entry point for everything Docker-based:
#   infra/docker/otel_setup.sh <otel_setup command>     e.g. enable, status, marketplace sync
#   infra/docker/otel_setup.sh collector install        render config, (re)create the Collector container
#   infra/docker/otel_setup.sh collector restart        same: re-render from .env and recreate
#   infra/docker/otel_setup.sh collector uninstall [--purge]   stop it (--purge also deletes volumes)
#   infra/docker/otel_setup.sh collector logs
# Run from anywhere; --dry-run is passed through and skips the Docker actions.
set -euo pipefail
cd "$(dirname "$0")"

export HOST_UID="$(id -u)" HOST_GID="$(id -g)" HOST_USER="$(id -un)" HOST_HOME="$HOME" HOST_NAME="$(hostname)"
ENV_FILE=../../.env
[[ -f $ENV_FILE ]] || { echo "error: $(cd ../.. && pwd)/.env not found. Copy .env.example to .env and fill it in." >&2; exit 2; }

mkdir -p "$HOME/.claude" "$HOME/.ssh"
docker network inspect otel-net >/dev/null 2>&1 || docker network create otel-net >/dev/null

compose() { docker compose --env-file "$ENV_FILE" -f docker-compose.yml "$@"; }

cli() {
  local extra=()
  if [[ -n "${SSH_AUTH_SOCK:-}" && -S "${SSH_AUTH_SOCK}" ]]; then
    export SSH_AUTH_SOCK_IN_CONTAINER=/ssh-agent
    extra+=(-v "${SSH_AUTH_SOCK}:/ssh-agent")
  fi
  compose build --quiet init
  compose run --rm "${extra[@]}" otel_setup "$@"
}

dry=false; for a in "$@"; do [[ $a == --dry-run ]] && dry=true; done

apply_collector() {  # re-render, validate, (re)create the container
  cli collector render "$@"
  $dry && return 0
  compose run --rm --no-deps -T collector validate --config=/var/lib/otel-setup/config/config.yaml
  compose up -d --force-recreate collector
}

case "${1:-} ${2:-}" in
  "collector install"|"collector restart")
    apply_collector "${@:3}" ;;
  "collector uninstall")
    if [[ " $* " == *" --purge "* ]]; then
      $dry && echo "[dry-run] would remove the Collector container and all otel volumes" || compose down --volumes
    else
      $dry && echo "[dry-run] would stop and remove the Collector container (volumes kept)" || compose down
    fi ;;
  "collector logs")
    compose logs -f collector ;;
  "marketplace sync")
    cli "$@"
    if ! $dry && [[ -n "$(compose ps -q collector)" ]]; then
      echo "applying the new allowlist to the running Collector..."
      apply_collector
    fi ;;
  *)
    cli "$@" ;;
esac
