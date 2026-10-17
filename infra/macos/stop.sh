#!/usr/bin/env bash
# macOS shutdown: run the shared steps. Use infra/stop.sh. When Docker is not running no
# container is either, so Docker is only started when --disable needs the otel_setup CLI.
set -euo pipefail

infra="$(cd "$(dirname "$0")/.." && pwd)"
source "$infra/lib/platform.sh"
source "$infra/macos/docker.sh"

if ! otel_wants_help "$@" && ! otel_docker_ready --no-launch; then
  disable=false
  for a in "$@"; do [[ $a == --disable ]] && disable=true; done
  if ! $disable; then
    echo "Docker is not running, so nothing is running; nothing to stop."
    exit 0
  fi
  otel_docker_ready
fi
exec "$infra/lib/stop-common.sh" "$@"
