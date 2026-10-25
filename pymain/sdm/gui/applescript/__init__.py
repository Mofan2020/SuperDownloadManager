"""AppleScript 支持（规格 §4.3 / 契约 §5.3）。

这里是普通包而不是命名空间包——**PyInstaller 对命名空间包的
支持不如普通包稳**，而这个包里的模块（`applescript_handler`）
必须在打包后的 .app 里能被 import 到。给它一个 `__init__.py`
是让打包这件事变成确定行为，而不是碰运气。

词典文件 `SuperDownloadManager.sdef` 不在这个包里——它要单独复制到
`Contents/Resources/`，`Info.plist` 的 `OSAScriptingDefinition`
指向那里。
"""
