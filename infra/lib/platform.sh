# Sourced by the infra scripts: host OS detection and dispatch to the per-OS scripts.
# Keep this (and everything under infra/) compatible with bash 3.2, the bash macOS ships:
# no associative arrays, no ${var,,}, and expand possibly-empty arrays as ${a[@]+"${a[@]}"}.

# Prints linux or macos; anything else is an error.
otel_os() {
  case "$(uname -s)" in
    Linux)  echo linux ;;
    Darwin) echo macos ;;
    *) echo "error: unsupported OS '$(uname -s)'; infra/ supports Linux and macOS (use WSL on Windows)" >&2
       return 2 ;;
  esac
}

# otel_dispatch <start|stop> [args...]: run infra/<os>/<action>.sh with the same arguments.
otel_dispatch() {
  local action=$1 infra os script
  shift
  infra="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  os="$(otel_os)" || exit 2
  script="$infra/$os/$action.sh"
  [[ -x $script ]] || { echo "error: $script not found or not executable" >&2; exit 2; }
  exec "$script" "$@"
}

# Checks shared by every OS: the docker CLI and the compose plugin are installed.
otel_require_docker_cli() {
  command -v docker >/dev/null || { echo "error: docker is required${1:+ ($1)}" >&2; exit 2; }
  docker compose version >/dev/null 2>&1 || { echo "error: the docker compose plugin is required" >&2; exit 2; }
}

# True when the arguments ask for --help (the per-OS scripts then skip their Docker checks).
otel_wants_help() {
  local a
  for a in "$@"; do [[ $a == -h || $a == --help ]] && return 0; done
  return 1
}

# Prints the --help text: the leading comment block of the given script.
otel_usage() { sed -n '2,/^set -euo/p' "$1" | sed '$d' | sed 's/^# \{0,1\}//'; }
