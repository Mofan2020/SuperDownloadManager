"""模拟「没有 PyObjC 的平台」——Linux 和 Windows 上跑 macOS 专属的测试。

**为什么需要这个。**

CI 的 matrix 里有 ubuntu-latest，但 macOS 专属的代码在 macOS 上
永远测不到平台差异。PyObjC 就是典型例子：

- macOS 上装了 PyObjC，`import Foundation` 成功
- Linux 上没装，同一行直接 `ModuleNotFoundError`

写测试的时候很容易只顾着当前平台能跑通——比如 fake 只注入了
`UserNotifications`，忘了 `Foundation` 也得注入。macOS 上真 PyObjC
兜住了，测试绿；Linux runner 上才炸。

**这个插件把 PyObjC 相关模块挡掉**，让 macOS 本机也能提前发现
这类问题。挡掉的模块：

- `objc`、`Foundation`、`CoreFoundation`、`AppKit`
- `UserNotifications`

抛的是 `ModuleNotFoundError`（不是 `ImportError`）——真 Linux 上缺
模块抛的就是这个，`pytest.importorskip` 只认它。

用法：

```bash
python -m pytest tests -p no_pyobjc
```

CI 里作为独立的一步跑，见 `.github/workflows/ci-python.yml`。
"""

from __future__ import annotations

import sys

#: 挡掉的模块。真 Linux 上这些都不存在。
BLOCKED = (
    "objc",
    "Foundation",
    "CoreFoundation",
    "AppKit",
    "UserNotifications",
)


class _PyObjCBlocker:
    """把这些模块的 import 变成 ModuleNotFoundError。"""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


def pytest_configure(config):
    """装上拦截器。

    放在 `sys.meta_path` 的最前面——比任何真实 finder 都早，
    所以 `import objc` 一定被挡住，不会走到真模块。
    """
    if any(isinstance(finder, _PyObjCBlocker) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PyObjCBlocker())
