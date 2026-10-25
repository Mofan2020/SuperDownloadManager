"""GUI 冒烟探针：验证菜单栏图标、JS API 桥、关窗不退出、退出不死锁。

**这是给 bug 排查用的，不是常规测试。** 之前出了三个问题：

1. 菜单栏图标装不上（NSStatusItem 建在了后台线程上）
2. 关窗后 Dock 图标不隐藏、点 Dock 图标不弹窗
3. 程序卡住退不出去（`global quitting` 写错，标志位没生效）

这些机器上能自动验，不必手点。跑法：

    python pymain/scripts/gui_probe.py

跑完打印一份结果。全绿退出码 0，有问题退出码 1。

**会真的开一个窗口**——pywebview 的 loaded 事件要等页面加载完才会触发。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: {detail}")


def main() -> int:
    import webview
    from PyObjCTools import AppHelper

    from sdm.gui import macos_status

    state = {"loaded": False, "js_api": None, "click_ok": None, "status_ok": None}

    def on_loaded() -> None:
        state["loaded"] = True

        def probe() -> None:
            # -- 菜单栏图标 ------------------------------------------
            try:
                controller = macos_status.StatusBarController(bridge=None, window=None)
                ok = controller.install()
                state["status_ok"] = ok
                # 装完必须有强引用，否则会被 GC 回收
                probe.controller = controller  # type: ignore[attr-defined]
            except Exception as err:
                state["status_ok"] = f"异常: {err}"

            # -- JS API 桥 + 侧边栏点击 ------------------------------
            try:
                script = """
                (function () {
                  const apiOk = !!(window.pywebview && window.pywebview.api);
                  const before = document.querySelector('.sidebar') ?
                    document.querySelector('.sidebar').innerHTML.length : -1;
                  const btn = document.querySelector('[data-view="history"], .sidebar button');
                  let clicked = null;
                  if (btn) {
                    btn.click();
                    clicked = document.querySelector('.view.active') ?
                      document.querySelector('.view.active').id : 'no-active-view';
                  }
                  return JSON.stringify({apiOk, before, clicked,
                                         buttons: document.querySelectorAll('.sidebar button').length});
                })()
                """
                raw = window.evaluate_js(script)
                state["js_api"] = raw
            except Exception as err:
                state["js_api"] = f"异常: {err}"

        AppHelper.callAfter(probe)

    window = webview.create_window(
        "SDM 探针",
        url=str(ROOT / "sdm" / "gui" / "assets" / "index.html"),
        js_api={},
        width=900,
        height=600,
    )
    window.events.loaded += on_loaded

    def watcher() -> None:
        # 等页面加载 + 探针跑完
        deadline = time.time() + 30
        while time.time() < deadline:
            if state["js_api"] is not None and state["status_ok"] is not None:
                break
            time.sleep(0.2)

        print("\n=== 探针结果 ===")

        check("页面加载完成", state["loaded"], f"loaded={state['loaded']}")

        status = state["status_ok"]
        check("菜单栏图标装上了", status is True, f"install() 返回 {status!r}")

        raw = state["js_api"]
        if isinstance(raw, str) and raw.startswith("{"):
            import json

            d = json.loads(raw)
            check("JS API 桥可用", d.get("apiOk") is True, f"window.pywebview.api={d.get('apiOk')}")
            check("侧边栏有按钮", (d.get("buttons") or 0) > 0, f"{d.get('buttons')} 个按钮")
            check("侧边栏点击有反应", d.get("clicked") not in (None, "no-active-view"),
                  f"点击后 active view = {d.get('clicked')}")
        else:
            check("JS API 桥可用", False, f"evaluate_js 返回 {raw!r}")

        # -- 关窗 + 退出 ---------------------------------------------
        print("\n=== 关窗 / 退出 ===")
        #
        # 模拟真实 app 的行为：closing 事件返回 False 取消关闭。
        # 之前 `global quitting` 写错，导致「退出」时标志位没生效，
        # closing 一直返回 False → destroy 被取消 → 程序卡死退不出去。
        quitting = {"value": False}

        def on_closing():
            if quitting["value"]:
                return None          # 正在退出，不拦
            return False             # 取消关闭，应用继续活着

        window.events.closing += on_closing

        t0 = time.time()
        try:
            window.hide()
            time.sleep(0.5)
            check("窗口能隐藏", True, f"{time.time() - t0:.2f}s")
        except Exception as err:
            check("窗口能隐藏", False, str(err))

        # 真正退出——之前这里会死锁（global quitting 写错，
        # closing 一直返回 False，destroy 被取消）
        quitting["value"] = True
        t1 = time.time()
        try:
            window.destroy()
            check("窗口能销毁（不退出会卡死）", True, f"{time.time() - t1:.2f}s")
        except Exception as err:
            check("窗口能销毁（不退出会卡死）", False, str(err))

    threading.Thread(target=watcher, daemon=True).start()

    webview.start()
    # webview.start() 返回说明窗口关了。
    time.sleep(0.3)

    ok = all(p for _, p, _ in results)
    print(f"\n=== {'全部通过' if ok else '有失败项'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
