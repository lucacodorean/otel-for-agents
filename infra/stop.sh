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
#
# Detects the host OS and runs infra/linux/stop.sh or infra/macos/stop.sh, which check
# that OS's Docker setup and then run the shared steps in infra/lib/stop-common.sh.
set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/lib/platform.sh"
otel_dispatch stop "$@"
