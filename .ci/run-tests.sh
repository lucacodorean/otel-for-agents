#!/usr/bin/env bash
# Single entry point for CI (GitHub Actions and GitLab CI) and for local runs:
#
#   .ci/run-tests.sh unit   [suite...]   # no Docker: pure Python tests
#   .ci/run-tests.sh docker [suite...]   # promtool, Collector, Prometheus, Grafana containers
#   .ci/run-tests.sh all    [suite...]
#
# suite: otel-setup, prometheus, grafana (default: all three). Extra pytest arguments go after
# `--`, e.g. `.ci/run-tests.sh unit otel-setup -- -k config`. Expects the package installed
# (pip install -e ".[dev]"); the docker mode fails instead of skipping when Docker is missing.
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${1:-}"
shift || true
case "$mode" in
  unit) marker=(-m "not docker") ;;
  docker) marker=(-m docker); export REQUIRE_DOCKER=1 ;;
  all) marker=(); export REQUIRE_DOCKER=1 ;;
  *) echo "usage: $0 unit|docker|all [otel-setup|prometheus|grafana ...] [-- pytest args]" >&2; exit 2 ;;
esac

suites=()
while (($#)) && [[ $1 != -- ]]; do
  [[ -d tests/$1 ]] || { echo "error: unknown suite '$1' (no tests/$1)" >&2; exit 2; }
  suites+=("tests/$1")
  shift
done
[[ ${1:-} == -- ]] && shift
((${#suites[@]})) || suites=(tests/otel-setup tests/prometheus tests/grafana)

mkdir -p reports
report="reports/junit-${mode}-$(IFS=-; echo "${suites[*]//tests\//}").xml"
exec python -m pytest ${marker[@]+"${marker[@]}"} --junitxml="$report" "${suites[@]}" "$@"
