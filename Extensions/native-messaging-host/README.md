# 原生消息宿主模板

**正常情况下你不需要动这个目录。** 应用首次启动时会自动：

1. 把打包好的 `sdmd-native-host` 放到 app 包里（macOS 是
   `Contents/Resources/native-host/`）；
2. 在 `~/Library/Application Support/SuperDownloadManager/native-host/`
   （Windows 是 `%LOCALAPPDATA%\SuperDownloadManager\native-host\`）
   写一个启动脚本；
3. 把启动脚本的**绝对路径**写进各浏览器的
   `NativeMessagingHosts/com.skyc8266.superdownloadmanager.json`。

装完浏览器扩展就能用。

## 什么时候需要手工装

- 你把 app 挪了位置，启动脚本里的路径失效了；
- 你想手动控制扩展 ID 的授权范围；
- 调试时想在命令行直接跑宿主。

## 手工装的步骤

1. 把 `sdmd-native-host.sh`（macOS）或 `sdmd-native-host.cmd`（Windows）
   复制到上面说的启动脚本目录，按你的实际路径改里面的 `exec` /
   `"<路径>"` 那一行；
2. 复制 `com.skyc8266.superdownloadmanager.json` 到各浏览器的
   `NativeMessagingHosts/` 目录，把 `path` 改成启动脚本的**绝对路径**；
3. 把 `allowed_origins` 里的 `REPLACE_WITH_EXTENSION_ID` 换成真的扩展 ID
   （`chrome://extensions` 里能看到）。

清单里写错扩展 ID 的表现是浏览器给一句含糊的
"Specified native messaging host not found"。

更详细的说明见 [`docs/extension-install.md`](../../docs/extension-install.md)。
