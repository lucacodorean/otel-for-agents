#!/usr/bin/env bash
# Bring the whole pipeline up (idempotent):
#   1. dev Prometheus (only when .env points at it)
#   2. Grafana (only when GRAFANA_ADMIN_PASSWORD is set in .env)
#   3. marketplace sync
#   4. Collector
#   5. Claude Code settings
#   6. status
#
#   infra/start.sh [--no-prometheus] [--no-grafana] [--no-sync] [--no-enable] [--dry-run]
#
# Detects the host OS and runs infra/linux/start.sh or infra/macos/start.sh, which check
# that OS's Docker setup and then run the shared steps in infra/lib/start-common.sh.
set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/lib/platform.sh"
otel_dispatch start "$@"
