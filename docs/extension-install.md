# 浏览器扩展安装指南 / Browser Extension Install

> 星空喵偏好："语言/教程简洁直接，不写叙事"。本文只讲**怎么把扩展装上**。

## Firefox 扩展（首选）

1. 打开 Firefox，访问 `about:debugging#/runtime/this-firefox`
2. 点击 **"Load Temporary Add-on..."**
3. 选择 `Extensions/Firefox/manifest.json`
4. 确认 SDM 的 LaunchAgent 已启动：`launchctl print gui/$(id -u) | grep com.superdownloadmanager`

> **持久化**：Firefox 临时扩展重启浏览器会失效。v1.0 在 README 列入手动 AMO 签名计划；目前每次开 Firefox 需要重新加载。
>
> **PM5 自动重载**：Firefox 提供 `browser.management` API 让扩展检测自身版本，可在 GUI 里"检测到更新 → 自动重载"—— v1.0 把这条放在 TODO。

## Safari 扩展（手动）

Safari 强制要求扩展由 Xcode 签发或在 macOS 15+ 启用"未签名扩展"白名单。

1. 打开 Safari → Settings → Advanced → 勾选 **"Show features for web developers"**
2. 打开 Develop 菜单
3. 选 "Allow Unsigned Extensions"（仅在按 Alt 进入 Develop 菜单时显示）
4. 然后 Develop → "Load Unpacked Extension..." → 选 `Extensions/Safari/`
5. Safari 设置 → Extensions 里启用 SDM

> **macOS 15+ 7 天提醒**：Safari 强制要求 macOS 15+ 用户每 7 天在 Safari Settings 里点击"Allow Extensions"重新授权。这是 Apple 强制的，我们改不了。
>
> **接管能力限制**：Safari Web Extension 在最新 macOS 不能拦截 `downloads.download` API。SDM Safari 扩展**只能**做悬停按钮嗅探；Firefox 才是真的接管。

## 验证接管工作

浏览器打开任意 100MB+ 文件下载链接，Firefox 上应该由 SDM 处理——下载文件会落在 `~/Downloads/<分类>/`。Safari 上悬停会出现紫色 `↓ SDM` 按钮。

如果 Firefox 没接管：

1. 确认 `[sdmd] listening` 日志能追到
2. `curl http://127.0.0.1:7955/ping` 应返回 `{"ok":true,"result":{"pong":true}}`
3. 看 Firefox 扩展的 `about:addons` 里 console 输出

## 给开发者的细节

- 派单端点：`http://127.0.0.1:7955/add?url=<encoded>&filename=<opt>&referer=<opt>`
- timeout 4 秒——超过 4s 我们认为派单失败
- 服务仅绑 127.0.0.1，不对外暴露
- 内容脚本 z-index 2147483647 — 应盖在所有元素之上

## 已知缺陷（v1.0）

| | |
|---|---|
| Firefox 临时扩展重启失效 | 一次性 AMO 签名是 v2 工作 |
| Safari 不接管下载 | Apple 设计，不可破 |
| `Extension/Firefox/icons/` 占位 | v2 加 SVG icons + chrome store 图标 |
