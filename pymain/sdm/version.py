"""版本与身份常量。版本号只在这里出现一次。"""

APP_NAME = "SuperDownloadManager"
APP_DISPLAY_NAME = "Super Download Manager"
__version__ = "2.0.0"
BUILD_NUMBER = 1

BUNDLE_ID = "com.skyc8266.superdownloadmanager"
AUTHOR = "Skyc8266"
COPYRIGHT = "Copyright © 2026 Skyc8266"

# 供旧版 sdmdm CLI 复用的 socket 路径（macOS / Linux）
SOCKET_PATH_UNIX = "~/Library/Application Support/SuperDownloadManager/sdmd.sock"
# Windows 没有 unix socket，用 TCP 回环端口
SOCKET_PORT_WINDOWS = 7956
DISPATCH_PORT = 7955
