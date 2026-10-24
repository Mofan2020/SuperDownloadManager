#!/bin/sh
# SDM v2.0 uninstall — removes the CLI link and unloads the daemon.
set -eu

LAUNCH_AGENT_LABEL="com.skyc8266.superdownloadmanager.daemon"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
CLI_LINK="${HOME}/.local/bin/sdm"

launchctl bootout "gui/$(id -u)" "${LAUNCH_AGENT_PLIST}" 2>/dev/null || true
rm -f "${LAUNCH_AGENT_PLIST}"
echo "[uninstall.sh] LaunchAgent unloaded: ${LAUNCH_AGENT_LABEL}"

if [ -L "${CLI_LINK}" ]; then
    rm -f "${CLI_LINK}"
    echo "[uninstall.sh] CLI link removed: ${CLI_LINK}"
fi
