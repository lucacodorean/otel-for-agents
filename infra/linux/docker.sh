# Sourced by infra/linux/start.sh and stop.sh: make sure the Docker engine is usable.
otel_docker_ready() {
  otel_require_docker_cli "https://docs.docker.com/engine/install/"
  local err
  err="$(docker info 2>&1 >/dev/null)" && return 0
  if [[ $err == *"permission denied"* ]]; then
    echo "error: no permission to use the Docker daemon. Add yourself to the docker group" >&2
    echo "       (sudo usermod -aG docker \"\$USER\", then log in again) or use rootless Docker." >&2
  else
    echo "error: the Docker daemon is not reachable. Start it (sudo systemctl start docker)." >&2
    echo "       docker said: $err" >&2
  fi
  exit 2
}
