"""版本与身份常量。版本号只在这里出现一次。"""

APP_NAME = "SuperDownloadManager"
APP_DISPLAY_NAME = "Super Download Manager"
__version__ = "2.2.9"
BUILD_NUMBER = 14

BUNDLE_ID = "com.skyc8266.superdownloadmanager"
AUTHOR = "Skyc8266"
COPYRIGHT = "Copyright © 2026 Skyc8266"

# daemon 的 socket 路径（macOS / Linux）。CLI 客户端 `sdm` 连这里。
SOCKET_PATH_UNIX = "~/Library/Application Support/SuperDownloadManager/sdmd.sock"
# Windows 没有 unix socket，用 TCP 回环端口
SOCKET_PORT_WINDOWS = 7956
# GUI 托管的 CLI 端点。
#
# **跟 7955 的 dispatch 端口不是一回事。** 7955 是给浏览器扩展用的
# 回环 HTTP 服务（有 CORS 头、要配对 token），协议完全不一样。
# 这里说的是 CLI 跟 GUI 说话的那条 IPC 通道——跟 daemon 用的是
# 同一套协议，只是端点不同。
#
# GUI 跑着的时候 CLI 连这里，任务直接进 GUI 的引擎，界面上立刻可见。
# GUI 没跑就退回 daemon（7956），任务照下，只是 GUI 打开后看不到。
GUI_CLI_PORT_WINDOWS = 7957
# 兼容旧常量名
DISPATCH_PORT = 7955
