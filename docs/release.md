# Release / 发布说明（开发者向）

按星空喵硬偏好："release notes 面向用户，本文件面向开发者。"

## 发版流程

1. **代码全部定稿**——最后一行代码改完才跑测试。
   改版本号、写发布说明、改文档这类"不影响代码"的改动，
   往往发生在跑完测试之后，于是漏掉了。

2. **同步版本号**。版本号散在多个文件里，改一处等于没改完：

   | 位置 | 内容 |
   |---|---|
   | `pymain/sdm/version.py` | `__version__` + `BUILD_NUMBER` |
   | `pymain/pyproject.toml` | `version` |
   | `Extensions/*/manifest.json` | 四个扩展各一份 `version` |
   | macOS `Info.plist` | 由打包脚本从 `version.py` 取 |

   `tests/test_app_icon.py` 里的**版本一致性测试**盯着前两项和四个
   扩展 manifest——跑一遍就知道有没有漏。

3. **跑全量测试**：`python3 -m pytest tests` + `python3 -m ruff check sdm tests scripts`。

4. **打包冒烟**（用干净 venv——本机 anaconda 同时装了 PyQt5 和
   PySide6，`build.py --mac` 在那个环境里必失败）：

   ```sh
   /tmp/sdm-build-venv/bin/python scripts/build.py --mac
   ```

   脚本自带冒烟：`--self-test`、CLI 启停、签名验证。

5. **打 tag**：`git tag -a vX.Y.Z -m "..."`，推到 main。

6. **等 CI 通过**（`ci-python.yml` 跑测试矩阵，`release-python.yml`
   出产物）。tag push 触发 `release-python.yml`，自动发 **Pre-release**。

7. **通知星空喵手测**，等回"通过"。

8. **升正式版**：`gh release edit vX.Y.Z --prerelease=false`。

未经星空喵手测确认的版本**只能 Pre-release**，即便 CI 全绿。

## 要点

- **tag 必须指向最终 commit。** 打 tag 之后再改代码，就得走一遍
  「取消已排队的 workflow run → 删远端 tag → 重打 tag → 再推」——
  **删 tag 不会自动取消已排队的 workflow**，GitHub 那边照跑，跑完
  还会把 release 补出来，结果是两份 release。
- **workflow 只发 Pre-release**，正式版靠 `gh release edit` 手动转。
- **辅助文件不进下载目录**——`.sdm.json` / `.sdm.session.json` /
  `.sdm.partial-N` 都在 `~/Library/Application Support/SuperDownloadManager/downloads/`。

## 版本历史

- **v2.x** —— Python 重写版（pywebview GUI + httpx 引擎）。
- **v1.x** —— macOS 原生版（已从仓库移除，代码在 git 历史里）。
  功能台账留在 [`functional-inventory.md`](./functional-inventory.md)，
  作为 Python 版的行为对照基准。
