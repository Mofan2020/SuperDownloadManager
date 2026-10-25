#!/bin/sh
# Super Download Manager 原生宿主启动脚本（macOS）。
#
# 浏览器的宿主清单里 `path` 指向这个脚本——浏览器只认清单里的
# `path`，不认可执行文件本身（macOS 上没有 .app 包装的二进制
# 直接跑不起来）。
#
# 真实脚本由 `sdm.ipc.native_host.write_host_launcher()` 在安装时
# 写出来，`exec` 后面跟的是打包好的 `sdmd-native-host` 的实际路径。
exec "/Applications/SuperDownloadManager.app/Contents/Resources/native-host/sdmd-native-host" "$@"
