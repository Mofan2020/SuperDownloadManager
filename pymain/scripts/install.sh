#!/bin/sh
# SDM v2.0 install — links the CLI and registers the launchd daemon.
# Idempotent: running it twice is harmless.
set -eu

APP_PATH="${1:-/Applications/SuperDownloadManager.app}"
LAUNCH_AGENT_LABEL="com.skyc8266.superdownloadmanager.daemon"
LAUNCH_AGENT_PLIST="${HOME}/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
CLI_LINK="${HOME}/.local/bin/sdm"

if [ ! -d "${APP_PATH}" ]; then
    echo "[install.sh] missing ${APP_PATH}; drag SuperDownloadManager.app into /Applications first." >&2
    exit 1
fi

# CLI 和 daemon 放在 Contents/Resources/ 下面，不是 Contents/MacOS/。
# 放 MacOS/ 下的话，codesign 会把 onedir 的 _internal 子目录当成
# 子 bundle，签名整个 .app 直接失败。Resources/ 是 codesign 当资源
# 处理的位置，不递归，签名能过。
BIN_DIR="${APP_PATH}/Contents/Resources"

# 1. Link the CLI into ~/.local/bin.
# CLI 是 onedir：可执行文件在 Contents/Resources/sdm-cli/sdm，
# 依赖在同级的 _internal/ 里。
CLI_BIN="${BIN_DIR}/sdm-cli/sdm"
if [ ! -x "${CLI_BIN}" ]; then
    echo "[install.sh] ${CLI_BIN} not found — reinstall the app." >&2
    exit 1
fi
mkdir -p "$(dirname "${CLI_LINK}")"
ln -sfn "${CLI_BIN}" "${CLI_LINK}"
echo "[install.sh] linked CLI: ${CLI_LINK}"

# 2. Write the LaunchAgent plist.
#    The daemon is its own binary (sdmd), not the GUI with an env var.
DAEMON_BIN="${BIN_DIR}/sdmd-daemon/sdmd"
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
    <key>EnvironmentVariables</key>
    <dict>
        <key>SDM_DAEMON</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>/tmp/${LAUNCH_AGENT_LABEL}.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/${LAUNCH_AGENT_LABEL}.err.log</string>
</dict>
</plist>
EOF

# 3. Boot the agent.
#    The target must be gui/<uid>/<label> — passing only the label
#    makes launchctl kickstart fail with "Unrecognized target specifier".
launchctl bootout "gui/$(id -u)" "${LAUNCH_AGENT_PLIST}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${LAUNCH_AGENT_PLIST}"
launchctl kickstart -k "gui/$(id -u)/${LAUNCH_AGENT_LABEL}"

echo "[install.sh] LaunchAgent loaded: ${LAUNCH_AGENT_LABEL}"
echo "[install.sh] verify with: launchctl print gui/\$(id -u) | grep ${LAUNCH_AGENT_LABEL}"
echo "[install.sh] verify CLI  with: ${CLI_LINK} ping"
