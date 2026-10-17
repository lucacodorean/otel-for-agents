# Containerised otel_setup CLI. The repository is bind-mounted at /app at run
# time, so code and template changes need no rebuild; the image only carries
# the runtime dependencies (python, bcrypt, git and ssh for private marketplaces).
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "bcrypt>=4"

# Run as the host user so files written to bind mounts (repo, ~/.claude) keep their
# owner, and ssh/git find a passwd entry for that uid. The host's primary group may
# already exist in the image (macOS: gid 20 "staff" is Debian's "dialout"); it is reused.
ARG HOST_UID=1000
ARG HOST_GID=1000
ARG HOST_USER=otel
RUN { getent group "${HOST_GID}" >/dev/null || groupadd -g "${HOST_GID}" "${HOST_USER}"; } \
 && useradd -u "${HOST_UID}" -g "${HOST_GID}" -m -d "/home/${HOST_USER}" "${HOST_USER}" \
 && mkdir -p /var/lib/otel-setup /var/cache/otel-setup \
 && chown "${HOST_UID}:${HOST_GID}" /var/lib/otel-setup /var/cache/otel-setup

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    OTEL_SETUP_IN_CONTAINER=1 \
    OTEL_SETUP_HOME=/var/lib/otel-setup \
    OTEL_SETUP_CACHE=/var/cache/otel-setup
WORKDIR /app
USER ${HOST_USER}
ENTRYPOINT ["python", "-m", "otel_setup"]
