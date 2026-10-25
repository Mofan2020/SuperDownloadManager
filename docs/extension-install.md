# 浏览器扩展安装

浏览器接管有两条通道（契约 §4）：

| 通道 | 用途 | 说明 |
|---|---|---|
| **Native Messaging**（主） | Chrome / Edge / Firefox | 浏览器拉起本机宿主进程，走 stdio 帧（4 字节小端长度 + JSON）。契约 §4.1。 |
| **回环 HTTP**（降级） | Safari / 没装宿主时 | `http://127.0.0.1:7955/add`，必须带配对 token。 |

两条通道都要求**配对 token**（契约 §4.2）。扩展第一次连上应用时用
`{"op":"pair"}` 换一个 token，存进 `browser.storage.local`，之后每条消息都带上。
应用侧不匹配就拒绝（`{"ok":false,"error":"unauthorized"}`）。

> 为什么要 token：这个回环端口带着 `Access-Control-Allow-Origin: *`，
> **任何网页**都能 `fetch("http://127.0.0.1:7955/add?url=...")`。
> 没有 token 的话，任何网站都能让用户的下载器去下它指定的东西。
> 因此 HTTP 通道**不开 `/pair`**——在那个端口发 token 等于把 token 贴到网上。

---

## 1. 装原生宿主（Chrome / Edge / Firefox）

原生宿主是应用的一部分。装好之后，扩展第一次连接会自动配对。

### 命令行装（调试用）

```python
from sdm.ipc import native_host

# 1) 写启动脚本 + 各浏览器的宿主清单
native_host.install_native_host(
    "/Applications/SuperDownloadManager.app/Contents/Resources/native-host/sdmd-native-host",
    extension_ids=["<Chrome 扩展 ID>"],   # 从 chrome://extensions 里复制
)

# 2) 看装到哪儿了
native_host.host_manifest_dirs()
```

`install_native_host()` 是幂等的，重复调用结果一样。清单写进这些目录：

**macOS**

- `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`
- `~/Library/Application Support/Chromium/NativeMessagingHosts/`
- `~/Library/Application Support/Microsoft Edge/NativeMessagingHosts/`
- `~/Library/Application Support/Mozilla/NativeMessagingHosts/`

**Windows**

- `%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\`
- `%LOCALAPPDATA%\Microsoft\Edge\User Data\NativeMessagingHosts\`
- `%APPDATA%\Mozilla\NativeMessagingHosts\`

每个目录里放一个 `com.skyc8266.superdownloadmanager.json`。**`path` 指向的是启动
脚本，不是宿主可执行文件本身**——`install_native_host()` 会在
`~/Library/Application Support/SuperDownloadManager/native-host/` 写一个
`sdm-native-host` 脚本，清单指向它，脚本再 `exec` 打包好的宿主：

```json
{
  "name": "com.skyc8266.superdownloadmanager",
  "description": "Super Download Manager 浏览器接管宿主",
  "path": "/Users/<你的用户名>/Library/Application Support/SuperDownloadManager/native-host/sdmd-native-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://<扩展 ID>/"]
}
```

> `path` **必须是绝对路径**——浏览器按字面路径执行，相对路径会按它自己的
> 工作目录解释，多半找不到。Windows 上是
> `%LOCALAPPDATA%\SuperDownloadManager\native-host\sdmd-native-host.cmd`。

Firefox 的清单多一个 `allowed_extensions`（写 gecko id `sdm-firefox@mofan2020.dev`）——
Firefox 的运行时 origin 是随机的 `moz-extension://<uuid>/`，写不死，只能按扩展 ID 授权。

> **Chrome / Edge 的扩展 ID 必须换成真的。** 商店安装的扩展 ID 是商店给的；
> 「加载已解压」的话在 `chrome://extensions` 里能看到。写错了浏览器的表现是
> 一句含糊的 "Specified native messaging host not found"。

### 图形界面装

应用里的「浏览器扩展」页面有按钮，走的是同一个 `install_native_host()`。

---

## 2. 装扩展

### Chrome / Edge

1. 打开 `chrome://extensions`（Edge 是 `edge://extensions`）
2. 右上角打开**开发者模式**
3. **加载已解压的扩展程序** → 选 `Extensions/Chrome/`（Edge 选 `Extensions/Edge/`）
4. 记下扩展 ID，安装原生宿主时要用（见上一节）

### Firefox

1. `about:debugging#/runtime/this-firefox`
2. **临时载入附加组件...** → 选 `Extensions/Firefox/manifest.json`

> 临时附加组件在 Firefox 重启后会消失，要重新载入。
> 正式分发要签名 XPI（需要 addons.mozilla.org 账号）。

### Safari（试验性）

Safari 的 WebExtension **没有自动接管下载的能力**（契约 §4.4.5），
只能手动发：右键菜单「发送到下载器」或工具栏按钮。界面上标了**试验性**。

1. Safari → 设置 → 高级 → 打开**显示网页开发者功能**
2. 开发菜单 → **允许未签名的扩展**（部分 macOS 版本要按 Option）
3. 开发 → **载入未签名的扩展...** → 选 `Extensions/Safari/`
4. Safari → 设置 → 扩展 → 启用 **Super Download Manager**

> **Safari 的原生消息不是 Chrome 那种宿主。** Safari 的
> `browser.runtime.sendNativeMessage(appId, msg)` 是发给应用包里的
> **Safari Web Extension Handler**（macOS App Extension，实现
> `beginRequest(with:)`）。本仓库 `sdm/ipc/native_host.py` 是 Chrome 协议的
> 宿主，Safari 用不上——Handler 没实现时，Safari 扩展退回回环 HTTP 通道
> （需要手动填配对 token，见下一节）。

---

## 3. 配对

- **装了原生宿主**：扩展启动时自动 `{"op":"pair"}`，应用把 token 交给它，
  扩展存进 `browser.storage.local`。用户不用管。
- **只能走 HTTP 通道**（Safari 没装 Handler、Firefox 没装宿主）：
  扩展的选项页（Chrome/Edge/Firefox）里可以手动填 token。
  token 在应用的设置里（`browser_pair_token`），也可以用
  `sdm.ipc.native_host.load_pair_token()` 读。

选项页：扩展详情页 → 「扩展选项」，或者右键扩展图标 → 选项。

---

## 4. 行为说明

### 自动接管（Chrome / Edge / Firefox）

下载开始**之前**，扩展在 `downloads.onDeterminingFilename` 里
`return { cancel: true }`，同时把请求交给应用下载——这样同一个文件不会被下两遍。

- **Cookie / User-Agent / Referer** 通过 `webRequest` 观察 + `cookies` 权限拿到，
  一起发给应用。少了它们很多站点直接给 403。
- **POST 体**由内容脚本钩住 `fetch` / `XMLHttpRequest` 截获
  （MV3 的 `webRequest` 拿不到请求体）。
- 派单失败（应用没开、token 不对、协议不支持）时，扩展用
  `downloads.download()` **把下载还给浏览器**——用户的文件不会凭空消失。

### `blob:` / `data:` 一律放行

这两种协议浏览器自己能下，下载器接不了（没有可重放的 HTTP 请求）。
扩展**不派单、也不取消浏览器的下载**，服务端也会回
`{"ok":false,"error":"unsupported_scheme"}` 让扩展知道该怎么处理。

> 以前这里没判断：扩展取消了浏览器的下载，Python 侧又拒收，
> **用户的文件直接没了**。这是本扩展最严重的一个 bug。

### Alt 逃生舱

**按住 Alt（macOS 是 Option）点击链接时不接管**，下载交给浏览器自己走。
内容脚本在点击那一刻就把 `altKey` 报给后台——后台的
`onDeterminingFilename` 是同步回调，等不了异步往返。

### 右键菜单

四个浏览器都有「发送到下载器（SDM）」右键菜单，可以手动发当前链接。

---

## 5. 验证

```sh
# 原生宿主（应用在跑的前提下）
python3 -m sdm.ipc.native_host
# → 用下面的字节喂给它，看回包
#
# 帧格式：4 字节小端长度 + JSON。长度**不包含**这 4 个字节本身。
# 手写前缀容易算错（`{"op":"ping"}` 是 13 字节 = \x0d，写成 \x0e
# 会被当成截断帧拒掉），所以让 python 自己算：
python3 -c 'import sys,struct; p=b"{\"op\":\"ping\"}"; sys.stdout.buffer.write(struct.pack("<I",len(p))+p)' \
  | python3 -m sdm.ipc.native_host | xxd
```

正常应该看到 `{"ok":true,"pong":true}`。

```sh
# 回环通道（需要配对 token）
curl -i -H 'X-SDM-Token: <token>' \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com/test.zip"}' \
     http://127.0.0.1:7955/add
```

正常应该返回 `{"ok":true,"result":{...,"task_id":"..."}}`，同时应用里多出一行任务。

不想装扩展、只想验证通道的话，直接调 `sdm.ipc.native_host.handle_message()`：

```python
from sdm.ipc.native_host import handle_message, ensure_pair_token

print(handle_message({"op": "add", "token": ensure_pair_token(),
                      "url": "https://example.com/test.zip"}))
```

---

## 6. 已知限制

- **Safari 只能手动派单**，而且整个扩展是试验性的：Safari 的扩展 API
  不提供「下载开始前拦一道」的钩子，硬做会丢文件。
- **Firefox 的原生宿主要单独装**。Firefox 的宿主清单按 gecko id 授权，
  不用管运行时随机 origin。
- **MV3 拿不到 `webRequestBlocking`**，所以扩展改不了请求头——
  我们本来也不需要改，只要读。
- **POST 体靠内容脚本截获**，页面用了 `sendBeacon` 之类不经过
  `fetch` / `XMLHttpRequest` 的路径就截不到（这时 `post_data` 是空的）。
- **Cookie 要 `cookies` 权限 + 主机权限**，隐私模式 / 第三方 cookie 被禁时拿不到。

---

## 7. 卸载

1. 浏览器里删掉扩展（Chrome/Edge：`chrome://extensions`；Firefox：
   `about:debugging`；Safari：设置 → 扩展）
2. 删掉宿主清单：
   - macOS：`~/Library/Application Support/{Google/Chrome,Chromium,Microsoft Edge,Mozilla}/NativeMessagingHosts/com.skyc8266.superdownloadmanager.json`
   - Windows：`%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\`、
     `%LOCALAPPDATA%\Microsoft\Edge\User Data\NativeMessagingHosts\`、
     `%APPDATA%\Mozilla\NativeMessagingHosts\` 下的同名文件
3. 启动脚本在 `~/Library/Application Support/SuperDownloadManager/native-host/`
   （Windows 在 `%LOCALAPPDATA%\SuperDownloadManager\native-host\`）
4. 想让旧 token 失效：删掉 `~/.local/share/SuperDownloadManager/browser_pair_token`
   （macOS 是 `~/Library/Application Support/SuperDownloadManager/browser_pair_token`）
   或在应用里重置配对
