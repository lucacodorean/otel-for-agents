#!/usr/bin/env bash
# Linux shutdown: check the Docker engine, then run the shared steps. Use infra/stop.sh.
set -euo pipefail

infra="$(cd "$(dirname "$0")/.." && pwd)"
source "$infra/lib/platform.sh"
source "$infra/linux/docker.sh"

otel_wants_help "$@" || otel_docker_ready
exec "$infra/lib/stop-common.sh" "$@"
