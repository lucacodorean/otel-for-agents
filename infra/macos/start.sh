#!/usr/bin/env bash
# macOS startup: make sure Docker Desktop (or OrbStack / Colima) is running, then run the
# shared steps. Use infra/start.sh. SSH agent forwarding for private marketplaces is set
# up by infra/docker/otel_setup.sh, which handles the macOS socket itself.
set -euo pipefail

infra="$(cd "$(dirname "$0")/.." && pwd)"
source "$infra/lib/platform.sh"
source "$infra/macos/docker.sh"

otel_wants_help "$@" || otel_docker_ready
exec "$infra/lib/start-common.sh" "$@"
