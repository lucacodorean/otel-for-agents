# Sourced by infra/macos/start.sh and stop.sh. On macOS the Docker engine runs in a VM
# (Docker Desktop, OrbStack or Colima) that is often not started yet.

otel_docker_up() { docker info >/dev/null 2>&1; }

# otel_docker_ready [--no-launch]: make sure the engine is usable, starting Docker Desktop or
# OrbStack and waiting for it when needed. With --no-launch, returns 1 instead of starting it.
otel_docker_ready() {
  otel_require_docker_cli "install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/"
  otel_docker_up && return 0
  [[ ${1:-} == --no-launch ]] && return 1

  local app=""
  if [[ -d /Applications/Docker.app || -d $HOME/Applications/Docker.app ]]; then app=Docker
  elif [[ -d /Applications/OrbStack.app || -d $HOME/Applications/OrbStack.app ]]; then app=OrbStack
  fi
  if [[ -z $app ]]; then
    echo "error: the Docker engine is not running. Start Docker Desktop, OrbStack or Colima (colima start)." >&2
    exit 2
  fi

  echo "Docker is not running; starting $app..."
  open -g -a "$app"
  local i
  for i in $(seq 1 60); do   # cold starts of the VM take up to a minute or two
    otel_docker_up && { echo "$app is up."; return 0; }
    sleep 2
  done
  echo "error: $app did not become ready within 2 minutes; start it by hand and re-run." >&2
  exit 2
}
