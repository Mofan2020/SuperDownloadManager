"""SuperDownloadManager —— Python 重写版。

**架构**：一个 Python 核心（`sdm/engine`，httpx 引擎）+ 一个 Web UI
（`sdm/gui`，pywebview 外壳 + HTML/CSS/JS 前端）+ 一层 IPC
（`sdm/ipc`，本地 socket / Windows 回环端口）。

## 关于代码注释里的「Swift 版」

本项目最早是 macOS 原生版（Swift）。**那个版本的源码已从仓库移除**，
现在仓库里只有 Python 版。但重构时逐个模块对照着原生版写，很多
注释里留着「对应 Swift 版 `Engine/Xxx.swift`」「Swift 版这里是死代码」
这类说明——**它们解释的是「为什么这个模块长成这样」，是重构决策的
依据，所以保留了**。

想看原生版源码：

```sh
git log --all -- Engine/          # 历史提交
git show <commit>:Engine/SpeedPolicy.swift
```

功能台账（原生版的完整功能清单，作为 Python 版的验收基准）在
[`docs/functional-inventory.md`](../../docs/functional-inventory.md)。
"""
