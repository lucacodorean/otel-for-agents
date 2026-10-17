#!/usr/bin/env bash
# Linux startup: check the Docker engine, then run the shared steps. Use infra/start.sh.
set -euo pipefail

infra="$(cd "$(dirname "$0")/.." && pwd)"
source "$infra/lib/platform.sh"
source "$infra/linux/docker.sh"

otel_wants_help "$@" || otel_docker_ready
exec "$infra/lib/start-common.sh" "$@"
