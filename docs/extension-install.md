# 浏览器接管：安装与使用

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

## 0. 应用里的「浏览器接管」板块

**用户不需要看这份文档也能装**：应用「设置 → 浏览器接管」板块里有

- **配对 token**（可复制、可重新生成）
- **每个浏览器的扩展目录**（点「打开扩展目录」会在访达/资源管理器里打开）
- **每个浏览器装没装**（应用自己扫扩展目录 + 查 `pluginkit` 得到的真实状态）
- **每个浏览器的安装入口**（点「打开扩展管理页」直接跳到浏览器的扩展页）

这个板块是**唯一**的安装入口文档来源——下面的步骤只是它内容的展开说明。

---

## 1. 装原生宿主（Chrome / Edge / Firefox）

原生宿主是应用的一部分。**应用启动时会自动装好**，不需要用户操作：
`bootstrap.py` 会把清单写进各浏览器的 `NativeMessagingHosts` 目录
（用户级目录，不需要管理员权限）。装不上也不影响下载功能——扩展还有
回环 HTTP 那条通道（走手动粘 token）。

清单写进这些目录：

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
`sdmd-native-host` 脚本，清单指向它，脚本再 `exec` 打包好的宿主：

```json
{
  "name": "com.skyc8266.superdownloadmanager",
  "description": "Super Download Manager 浏览器接管宿主",
  "path": "/Users/<你的用户名>/Library/Application Support/SuperDownloadManager/native-host/sdmd-native-host",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://bangcjehjdodcpfekmnkfmbeffcclpih/"]
}
```

> `path` **必须是绝对路径**——浏览器按字面路径执行，相对路径会按它自己的
> 工作目录解释，多半找不到。Windows 上是
> `%LOCALAPPDATA%\SuperDownloadManager\native-host\sdmd-native-host.cmd`。

Firefox 的清单多一个 `allowed_extensions`（写 gecko id `sdm-firefox@mofan2020.dev`），
**而且没有** `allowed_origins`——Firefox 的运行时 origin 是随机的
`moz-extension://<uuid>/`，写不死，只能按扩展 ID 授权。

### Chrome / Edge 的扩展 ID 是固定的

`allowed_origins` 里的扩展 ID **必须跟扩展实际的 origin 精确一致**，
否则 Chrome 直接拒绝拉起宿主（表现是一句含糊的
"Access to the specified native messaging host is forbidden"）。

所以 Chrome / Edge 的 manifest 里写死了一把公钥（`key` 字段）：本地加载的
扩展，ID 由这把公钥算出来，**跟装在哪台机器、哪个目录都无关**。

```
扩展 ID = SHA256(公钥 DER 字节) 的前 32 个十六进制位，每个位 0-f 映射成 a-p
```

当前值：`bangcjehjdodcpfekmnkfmbeffcclpih`（Chrome 和 Edge 同一个）。

> **升级扩展时不要换这把公钥。** 一换 ID 就变了，所有机器上装好的宿主
> 清单全部失效。`tests/test_extensions_manifests.py` 里有一条测试专门盯着
> 「manifest 里的 key 算出来的 ID」和「代码里写死的 ID」是否一致。

---

## 2. 装扩展

扩展文件跟着应用一起打包：

- macOS：`SuperDownloadManager.app/Contents/Resources/extensions/<浏览器>/`
- Windows：GUI 是 onefile，解包到临时目录，路径由 `sys._MEIPASS/extensions/`

**别去源码仓库里找**——应用「设置 → 浏览器接管 → 打开扩展目录」直接
在访达/资源管理器里打开正确的那个目录，从那儿选。

### Chrome / Edge

1. 打开 `chrome://extensions`（Edge 是 `edge://extensions`）
   ——应用里点「打开扩展管理页」会直接跳过去
2. 右上角打开**开发者模式**
3. **加载已解压的扩展程序** → 选应用给的那个 **Chrome**（Edge 选 **Edge**）目录
4. 装完扩展 ID 应该是 `bangcjehjdodcpfekmnkfmbeffcclpih`。**不是这个值就说明
   装错目录了**（装了 Edge 的目录，或者 manifest 里的 `key` 被改过）

装完就能用：扩展第一次连接会通过原生宿主自动 `{"op":"pair"}` 配对。

### Firefox

1. `about:debugging#/runtime/this-firefox`
2. **临时载入附加组件...** → 选应用给的那个 **Firefox** 目录里的 `manifest.json`

> 临时附加组件在 Firefox 重启后会消失，要重新载入。
> 正式分发要签名 XPI（需要 addons.mozilla.org 账号）。

Firefox 的扩展 ID 是 manifest 里写死的 gecko id
（`sdm-firefox@mofan2020.dev`），跟 Chromium 系那把公钥没关系。

### Safari（试验性）

Safari 的 WebExtension **没有自动接管下载的能力**（契约 §4.4.5），
只能手动发：右键菜单「发送到下载器」或工具栏按钮。界面上标了**试验性**。

**本项目不提供签名**（没有 Apple Developer Program 账号），走 Apple 官方的
「未签名的临时扩展」路径。macOS 17 / Safari 17 起：

1. **Safari → 设置 → 高级** → 勾选 **「显示网页开发者功能」**
2. **Safari → 设置 → 开发者** → 勾选 **「允许未签名的扩展」**
3. **Safari → 设置 → 开发者** → 点 **「添加临时扩展…」**
   → 选应用给的那个 **Safari** 扩展目录（选目录，不是选 manifest.json）
4. **Safari → 设置 → 扩展** → 勾选启用 **Super Download Manager**
5. 点扩展图标展开 **「配对设置」**，把应用「设置 → 浏览器接管」里的
   **配对 token** 粘进去 → 「保存 token」
   （也可以在扩展的选项页里填，路径：Safari → 设置 → 扩展 → 选中该扩展 → 齿轮/选项）

**不需要** Xcode，**不需要** Apple 开发者账号。

> **平台的硬限制，改不掉：**
>
> - 「允许未签名的扩展」**退出 Safari 就重置**，下次要重新勾
> - 临时扩展**最长 24 小时**，过期或退出 Safari 就被移除，要重新「添加临时扩展…」
> - Safari 没有 Chrome 那种原生消息宿主（`browser.runtime.sendNativeMessage`
>   发给的是 Safari Web Extension Handler，本仓库没实现），所以 **Safari 必须
>   手动粘一次 token**
> - Safari 没有「下载开始前拦一道」的钩子，**只能手动派单**。强行自动接管
>   只能等下载开始后再取消——那时候文件已经在路上，用户会丢文件

---

## 3. 配对

- **Chrome / Edge / Firefox（装了原生宿主）**：扩展启动时自动 `{"op":"pair"}`，
  应用把 token 交给它，扩展存进 `browser.storage.local`。用户不用管。
- **Safari**：必须手动把 token 粘进扩展（弹窗里的「配对设置」折叠区，
  或扩展的选项页）。token 在应用的「设置 → 浏览器接管」里，
  也可以用 `sdm.ipc.native_host.load_pair_token()` 读。
- **任何浏览器**：原生宿主没装起来时，都可以在扩展的选项页里手动填 token
  （Chrome/Edge/Firefox 扩展详情页 → 「扩展选项」，或者右键扩展图标 → 选项）。

应用里点「重新生成」会立刻作废旧 token（设置文件和 token 存档两边都写，
`load_pair_token()` 是「设置文件优先」——只写一边下次读回来的还是旧的）。

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
- **Safari 的临时扩展 24 小时失效**，退出 Safari 就被移除，「允许未签名的
  扩展」也会重置。这是平台设计，不是工程问题。
- **Firefox 的临时附加组件重启后消失**，要重新载入。
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
4. 想让旧 token 失效：在应用里点「重新生成」，或者删掉
   `~/Library/Application Support/SuperDownloadManager/browser_pair_token`
   （Windows 在 `%LOCALAPPDATA%\SuperDownloadManager\browser_pair_token`）
