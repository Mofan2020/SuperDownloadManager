#!/bin/sh
# SDM uninstall — reverses install.sh.
set -eu

LAUNCH_AGENT_LABEL="com.superdownloadmanager.daemon"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
CLI_LINK="${HOME}/.local/bin/sdmdm"
APP_PATH="${1:-/Applications/SuperDownloadManager.app}"

if [ -f "${LAUNCH_AGENT_PLIST}" ]; then
    launchctl bootout "gui/$(id -u)" "${LAUNCH_AGENT_PLIST}" 2>/dev/null || true
    rm -f "${LAUNCH_AGENT_PLIST}"
    echo "[uninstall.sh] LaunchAgent removed"
fi
if [ -L "${CLI_LINK}" ]; then
    rm -f "${CLI_LINK}"
    echo "[uninstall.sh] CLI symlink removed"
fi
echo "[uninstall.sh] (app bundle at ${APP_PATH} left in place; remove manually if desired)"
