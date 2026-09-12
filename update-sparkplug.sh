#!/usr/bin/env bash
set -euo pipefail

# 1. Detect compose command (prefer podman compose if available, fallback to podman-compose)
if podman compose --help &>/dev/null; then
    COMPOSE="podman compose"
elif command -v podman-compose &>/dev/null; then
    COMPOSE="podman-compose"
else
    COMPOSE="podman compose"
fi

# 2. Setup rootless environment variables if missing in non-interactive SSH
USER_ID="$(id -u)"
if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    if [ -d "/run/user/${USER_ID}" ]; then
        export XDG_RUNTIME_DIR="/run/user/${USER_ID}"
    fi
fi
if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    if [ -S "${XDG_RUNTIME_DIR}/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    fi
fi
export PATH="/usr/local/bin:/usr/bin:/bin:~/.local/bin:${PATH:-}"

# 3. Pull new image of this service
IMAGE="${SPARKPLUG_IMAGE:-ghcr.io/sairam-suresh/sparkplug:latest}"
echo "==> 1. Pulling new image: ${IMAGE}..."
podman pull "${IMAGE}"

# 4. Detach compose restart so SIGHUP does not kill it when sparkplug container stops
echo "==> 2. Scheduling clean container restart..."
LOG_FILE="/opt/serve/sparkplug/update.log"

nohup bash -c '
    exec >> "'"${LOG_FILE}"'" 2>&1
    echo "=== Update started at $(date) ==="
    # Allow runner SSH session 2s to flush and return cleanly before container stops
    sleep 2
    cd /opt/serve/sparkplug
    echo "==> Executing: '"${COMPOSE}"' down..."
    '"${COMPOSE}"' down --remove-orphans || '"${COMPOSE}"' down || true
    sleep 2
    echo "==> Executing: '"${COMPOSE}"' up -d..."
    '"${COMPOSE}"' up -d
    echo "==> Status after restart:"
    '"${COMPOSE}"' ps || true
    echo "=== Update completed successfully at $(date) ==="
' </dev/null >/dev/null 2>&1 &

echo "==> Compose restart successfully scheduled in background."
echo "==> Logs are streaming to: ${LOG_FILE}"
echo "==> Runner will now complete cleanly before containers restart."

