#!/bin/bash
set -euo pipefail

BOT_USER="botuser"
REPO_DIR="/home/botuser/.link-project-to-chat/repos/link-project-to-chat"
SERVICE_NAME="link-project-to-chat"
MANAGER_MATCH="link-project-to-chat start-manager"

install_editable() {
  python3 -m pip install --user -e "${REPO_DIR}[all]" --break-system-packages -q --no-warn-script-location
}

restart_via_systemd() {
  systemctl restart "${SERVICE_NAME}"
  echo "Rebuilt and restarted."
}

restart_via_signal() {
  local manager_pid
  manager_pid="$(pgrep -u "${BOT_USER}" -f "${MANAGER_MATCH}" | head -n 1 || true)"
  if [[ -z "${manager_pid}" ]]; then
    echo "Install completed, but no running manager process was found to restart." >&2
    echo "Restart ${SERVICE_NAME} manually." >&2
    exit 1
  fi
  echo "Install completed; asking systemd to restart ${SERVICE_NAME}."
  kill -TERM "${manager_pid}"
}

if [[ "$(id -u)" -eq 0 ]]; then
  sudo -u "${BOT_USER}" python3 -m pip install --user -e "${REPO_DIR}[all]" --break-system-packages -q --no-warn-script-location
  restart_via_systemd
  exit 0
fi

if [[ "$(id -un)" != "${BOT_USER}" ]]; then
  echo "Run rebuild.sh as root or ${BOT_USER}." >&2
  exit 1
fi

install_editable
restart_via_signal
