# Release / 发布说明（开发者向）

按星空喵硬偏好："release notes 面向用户，本文件面向开发者。"

## v1.0.0 — 计划目标

- macOS 26.0+ 部署目标
- Swift 5.9 语言模式，Xcode 27 SDK
- ad-hoc 签名本地运行
- 完整功能：见 README.md

## 发版流程（沿用发版规则）

1. `git tag vX.Y.Z` 推到 main
2. 等 CI 通过（xcodebuild Release 0 warning，swift test 全过）
3. tag push 触发 release workflow，生成 draft release
4. **立刻 `gh release edit vX.Y.Z --prerelease`**
5. 通知星空喵手测，等他回 "通过"
6. `gh release edit vX.Y.Z --prerelease=false` 升正式版

未经星空喵手测确认的版本**只能 Pre-release**，即便 CI 全绿。

详细规则见 [`../sdm-v1.0.md` P8 任务](./sdm-v1.0.md)。
