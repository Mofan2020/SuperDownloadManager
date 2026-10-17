#!/bin/sh
# SDM v1.0 install — registers launchd + symlinks the CLI.
# Idempotent.
set -eu

APP_PATH="${1:-/Applications/SuperDownloadManager.app}"
LAUNCH_AGENT_LABEL="com.superdownloadmanager.daemon"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
CLI_LINK="${HOME}/.local/bin/sdmdm"

if [ ! -d "${APP_PATH}" ]; then
    echo "[install.sh] missing ${APP_PATH}; drag SuperDownloadManager.app into /Applications first." >&2
    exit 1
fi

# 1. Symlink sdmdm into ~/.local/bin.
mkdir -p "${HOME}/.local/bin"
ln -sfn "${APP_PATH}/Contents/MacOS/sdmdm" "${CLI_LINK}"
echo "[install.sh] linked CLI: ${CLI_LINK}"

# 2. Write LaunchAgent plist — point at SuperDownloadManager binary
#    with SDM_DAEMON=1 (see Daemon/main.swift).
DAEMON_BIN="${APP_PATH}/Contents/MacOS/SuperDownloadManager"
mkdir -p "${HOME}/Library/LaunchAgents"
cat > "${LAUNCH_AGENT_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${DAEMON_BIN}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SDM_DAEMON</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/${LAUNCH_AGENT_LABEL}.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/${LAUNCH_AGENT_LABEL}.err.log</string>
</dict>
</plist>
EOF

# 3. Boot the agent.
launchctl bootout "gui/$(id -u)" "${LAUNCH_AGENT_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENT_PLIST}"
launchctl kickstart -k "gui/$(id -u)/${LAUNCH_AGENT_LABEL}"

echo "[install.sh] LaunchAgent loaded: ${LAUNCH_AGENT_LABEL}"
echo "[install.sh] verify with: launchctl print gui/\$(id -u) | grep ${LAUNCH_AGENT_LABEL}"
echo "[install.sh] verify CLI  with: ${CLI_LINK} ping"
