# 没动的与原因 / Deliberately Omitted

按星空喵硬偏好："项目文档是给所有维护者看的，包括人类，不要把只对 AI 助手有意义的指令写进仓库文档"。本文件只记录**项目级决策与未做事项 + 原因**，给任何人类/AI 维护者看。

## v1.0.0 状态

仓库里的 v1.0.0 已经由 GitHub Actions 发布为 **Pre-release**：

> https://github.com/Mofan2020/SuperDownloadManager/releases/tag/v1.0.0

asset 是 `SuperDownloadManager.app.zip`（ad-hoc 签名、解压拖 `/Applications` 即可）。按星空喵硬性偏好 **未经手测只发 Pre-release** ——星空喵下载手测通过后，agent 把 `gh release edit v1.0.0 --prerelease=false`。

## v1.0 暂不做

| # | 项 | 原因 |
|---|---|---|
| 1 | Live2D 模型接管 | Apple cubism SDK 法律约束；非工程能力 |
| 2 | 3D 模型预览 | macOS 原生 3D 能力够但渲染工作量大；P2 留口子 |
| 3 | BT / Magnet 下载 | 与本项目"普通文件下载"定位冲突；上 BT 意味着补 tracker 协议 + DHT + 跨网 NAT，半年工作量 |
| 4 | 自动解包 zip / 自动挂载 dmg | 安全顾虑 — 自动执行任意归档内容会把用户机器信任半径拉大；v2 视用户呼声决定 |
| 5 | 公网 URL 上传 / 分享 | 隐私问题 — SDM 持有下载链接 = 私密行为，外泄不可接受 |
| 6 | 多账号云同步 | 同上，下载历史属私密；本地 SQLite 足够 |

## v1.0 已知的妥协

| # | 项 | 妥协 vs 影响 |
|---|---|---|
| 1 | **Safari 浏览器接管能力有限** | macOS 15+ Safari Web Extension 强制"7 天重签"是平台设计，非工程问题；我们交付嗅探+悬停按钮接管入口。**Safari 用户点按钮手动派单** |
| 2 | **ad-hoc 签名本地跑** | Developer ID 是 v2/v3 目标；v1.0 走 ad-hoc + 用户自信任，DMG 安装需用户手动 "Open Anyway"。等星空喵购入开发者证书再升 |
| 3 | **动态分段策略激进版** | 初版合并阈值 0.3、分裂阈值 1.5，可通过 GUI 切保守；实测数据后迭代 |
| 4 | **无 CLI 的 `sdmd` 子进程实际启动** | P0 只声明源码层 stub，`@main` 入口在 P4 真正的 `sdmd` target 上写 |
| 5 | **GUI 截图实测** | TCC 限制（Screen Recording / Accessibility）下，agent session 无权限截图；GUI 验证落到星空喵手测。CLI 端到端测试 agent 可跑 |
| 6 | ~~Little Snitch + ad-hoc 签名应用的出站 0 字节~~ → 收回（误判） | 2026-10-19 agent 第一次排查时，把"URLSession 卡 60 s timeout"归因到 Little Snitch 默认拦截 ad-hoc 应用出站；星空喵现场确认 Little Snitch 模式 Silent Allow + SDM 实际请求未被拦截、规则无异样。收回这条，留作未来参考：URLSession 卡死现象 ≠ Little Snitch。**真因见 #7** |

| 7 | **`release-assets.githubusercontent.com` Range 请求在 SDM 里 0 KB/s，curl 直连同 URL 30 s 拿到 1 MB** | 实测（2026-10-19）：`curl -r 0-2097151` 拉用户给的 UCopy.exe URL，30 秒拿到 1,017,794 / 2,097,152 字节（33 KB/s 真实带宽上限，Content-Range = `bytes 0-2097151/13091795`、Content-Length = 2097152）。同一 URL 在 macOS 上 `URLSession.data(for: req)`（专属 session + IgnoreSSLAuthChallengeDelegate bypass）卡 60 s 后 timeout `kCFStreamErrorCodeKey=-2102`。端到端测试 (`Tests/EngineTests/DownloadFlowEndToEndTests.swift`) 只覆盖本地 Python range server，不覆盖公网 HTTPS + Fastly + 大文件 + chunk 并发，所以这条问题在 CI 单测里**完全没人守门**。**这是 1.x 整个产品生命周期的真因：URLSession.data(for:) 在某些网络条件下跟 HTTP/2 CDN（Fastly）出现 stream reset / 累积延迟**，但 SDM 内部没有任何兜底（不切换到流式 `URLSession.bytes`、不启用备用单流模式、不暴露真实状态给 UI）。**预期修复方向**（agent 不擅自动手）：(a) 给 chunk worker 加 `URLSession.bytes` 路径作为 data(for:) 卡死超过 N 秒的 fallback；(b) 把"实际进度"从 in-memory `progressChannel` 改成"按 partial 文件磁盘大小定期 poll"（v1.0.5 之前干过，被干掉了，但 v1.0.5 的 in-memory channel 自身就有 coalesce 截留 bug，50 ms 窗口里 chunk 没完成就不向 UI 发报告）；(c) 给 UI 加"已用时/已下字节/最后状态"日志让用户能立刻看到不是真 0 KB/s 而是卡在哪个 chunk |

## 借鉴 / 算法事实

| # | 项 | 来源 | 声明位置 |
|---|---|---|---|
| - | (P1 起) Range 合并 / 分裂策略 | 行业通用算法，IDM/aria2/FreeDownloadManager 均有类似实现 | 文件顶部 + commit + 此文档 |

## 参考

- [macos-native-app-development skill](../../../../../.hermes/skills/software-development/macos-native-app-development/SKILL.md) — 所有 macOS 工程的硬规矩底本
- [`sdm-v1.0.md`](./sdm-v1.0.md) — v1.0 主线计划
