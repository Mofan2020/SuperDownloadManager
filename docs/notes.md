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
| 6 | **Little Snitch + ad-hoc 签名应用的出站 0 字节** | 实测：2026-10-19 `release-assets.githubusercontent.com`（GitHub release CDN，DNS 解析到 `185.199.108-111.133` Fastly）curl 直连 0.4 s 通 200，URLSession / NWConnection 同一个进程内拉同一 URL 卡到 60 s timeout（`kCFStreamErrorCodeKey=-2102, kCFStreamErrorDomainKey=4`）。根因：Little Snick NetworkExtension 默认拦截未在规则表里的进程出站请求（ad-hoc 签名应用 `SDM.app` 与 `swiftc` 编译的临时二进制都属于这一类）；curl/nc 因为 Apple 签名 + 长期 allow 自动放行。**这不是 engine bug**，但在 ad-hoc + Little Snick 环境下表现为"下载速度 0 KB/s、UI 无错"与"以前修了几次还是这样"。**验证 / 临时绕开**：Little Snick → Rules → 找到 SuperDownloadManager → Connect Mode = Allow Once / Allow Any Time。**工程代码不用改**——已发布的 v1.1.8 忽略 SSL 选项对这条 URL 不相关，是 Little Snick 根本没把连接交给 curl 外的栈 |

## 借鉴 / 算法事实

| # | 项 | 来源 | 声明位置 |
|---|---|---|---|
| - | (P1 起) Range 合并 / 分裂策略 | 行业通用算法，IDM/aria2/FreeDownloadManager 均有类似实现 | 文件顶部 + commit + 此文档 |

## 参考

- [macos-native-app-development skill](../../../../../.hermes/skills/software-development/macos-native-app-development/SKILL.md) — 所有 macOS 工程的硬规矩底本
- [`sdm-v1.0.md`](./sdm-v1.0.md) — v1.0 主线计划
