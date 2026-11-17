"""对单实例守卫做变异测试。

**测了不等于测到了。** 这一份脚本把守卫的每一条判定改坏，看测试会不会
红。改法是「按字面片段替换」，所以脚本本身先检查片段还在——不在了就
报「脚本坏了」，那不是「变异存活」。

规矩：

- 每次只改一个地方，改完跑一次测试；
- 红了说明测试抓到了这个变异；
- 绿了说明测试是假的，得补测试；
- **还原必须放在 finally 里**：脚本被杀掉了，留在源码里的变异代码会
  让下一轮跑测试直接红，而且没人知道是被变异过的；
- 跑之前先确认没有别的 pytest 在跑（它读的是半改半没改的文件）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "sdm" / "gui" / "single_instance.py"
MAIN = REPO / "sdm" / "gui" / "main.py"
TRAY = REPO / "sdm" / "gui" / "tray.py"
TESTS = REPO / "tests" / "test_single_instance.py"
TESTS2 = REPO / "tests" / "test_second_instance.py"
TESTS3 = REPO / "tests" / "test_tray_watchdog.py"
JOURNAL = REPO / ".mutation-journal.json"
LOCK = REPO / ".mutation-single-instance.lock"

PYTHON = sys.executable

#: (名字, 文件, 原文, 改坏之后)
MUTATIONS: list[tuple[str, Path, str, str]] = [
    (
        "锁不写启动时间",
        SOURCE,
        """        text = str(os.getpid())
        if self._started is not None:
            text += f"\\n{self._started:.3f}\"""",
        """        text = str(os.getpid())""",
    ),
    (
        "启动时间对不上也算活着",
        SOURCE,
        "        return abs(actual - recorded) <= _START_TOLERANCE",
        "        return True",
    ),
    (
        "主人死了也算活着",
        SOURCE,
        "        if not _process_alive(pid):\n            return False",
        "        if not _process_alive(pid):\n            return True",
    ),
    (
        "旧锁也去比启动时间",
        SOURCE,
        "        if not isinstance(recorded, float):\n            return True",
        "        if not isinstance(recorded, float):\n            return False",
    ),
    (
        "主人活着也接管",
        SOURCE,
        "        if self._path.exists() and self.owner_alive():\n            return False",
        "        if self._path.exists() and self.owner_alive():\n            pass",
    ),
    (
        "释放时把别人的锁也删掉",
        SOURCE,
        "        if owner_pid and owner_pid != os.getpid():\n            return",
        "        if owner_pid and owner_pid != os.getpid():\n            pass",
    ),
    (
        "陈旧的锁也不接管",
        SOURCE,
        "        if self.owner():\n            return not self.owner_alive()",
        "        if self.owner():\n            return False",
    ),
    (
        "读不出内容的锁立刻接管",
        SOURCE,
        "        return age > STALE_SECONDS",
        "        return True",
    ),
    (
        "叫不醒也算叫醒了",
        SOURCE,
        "        return bool(reply.get(\"ok\"))\n\n    def hand_over_url",
        "        return True\n\n    def hand_over_url",
    ),
    (
        "地址没交出去也算交了",
        SOURCE,
        "        return bool(reply.get(\"ok\"))",
        "        return True",
    ),
    (
        "空地址也去交",
        SOURCE,
        "        if not url:\n            return True",
        "        if not url:\n            return False",
    ),
    (
        "叫醒之后也去接管",
        MAIN,
        '    if instance.ask_running_to_show():\n        log.info(CAT_GUI, "已经有一个实例在跑，已让它把窗口叫到前面来")\n        return "exit"',
        '    if instance.ask_running_to_show():\n        log.info(CAT_GUI, "已经有一个实例在跑，已让它把窗口叫到前面来")\n        instance.take_over()\n        return "exit"',
    ),
    (
        "叫不醒就不接管",
        MAIN,
        '    if instance.owner_alive():\n        log.warning(CAT_GUI, "已运行的实例没有应答，强制接管（可能会有两个图标）")\n    else:\n        log.info(CAT_GUI, "已运行的实例已经不在了，接管")\n    instance.take_over()\n    return "continue"',
        '    if instance.owner_alive():\n        log.warning(CAT_GUI, "已运行的实例没有应答，强制接管（可能会有两个图标）")\n    else:\n        log.info(CAT_GUI, "已运行的实例已经不在了，接管")\n    return "continue"',
    ),
    (
        "先叫窗口再交地址",
        MAIN,
        '    if pending_url:\n        instance.hand_over_url(pending_url)\n\n    # 再试试让它把窗口叫到前面来。\n    if instance.ask_running_to_show():',
        '    if instance.ask_running_to_show():\n        pending_url = pending_url\n    if pending_url:\n        instance.hand_over_url(pending_url)\n\n    if False:',
    ),
    (
        "图标掉了也不挂回去",
        TRAY,
        "        present = _icon_present(self._icon)\n        if present is False:\n            self._readd(\"图标不在通知区域了\")",
        "        present = _icon_present(self._icon)\n        if present is True:\n            self._readd(\"图标不在通知区域了\")",
    ),
    (
        "问不出来也挂回去",
        TRAY,
        "        present = _icon_present(self._icon)\n        if present is False:",
        "        present = _icon_present(self._icon)\n        if present is not None:",
    ),
    (
        "任务栏重建了也不挂回去",
        TRAY,
        '        if hwnd and hwnd != self._tray_hwnd:\n            # 任务栏被重建过了。\n            self._tray_hwnd = hwnd\n            self._readd("任务栏重建过")\n            return',
        "        if hwnd and hwnd != self._tray_hwnd:\n            # 任务栏被重建过了。\n            self._tray_hwnd = hwnd\n            return",
    ),
    (
        "重挂时只置 True",
        TRAY,
        "            self._icon.visible = False\n            self._icon.visible = True",
        "            self._icon.visible = True",
    ),
    (
        "重挂失败就把看门狗带崩",
        TRAY,
        "        except Exception as err:\n            log.warning(CAT_GUI, f\"托盘图标重新挂上失败: {err}\")",
        "        except Exception as err:\n            raise",
    ),
]


def _run_tests() -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(
        [
            PYTHON,
            "-m",
            "pytest",
            str(TESTS),
            str(TESTS2),
            str(TESTS3),
            "-q",
            "--timeout=60",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _acquire_lock() -> int:
    """抢锁。

    **同一棵源码树上只能有一个变异跑。** 两个一起跑就互相把对方的变异
    当成原始内容：后启动的那个报「片段不在」，先启动的那个把对方的
    改动当成自己的改动。两边结论的前提都是错的。

    锁里写进程号。进程还在就等着，进程没了（上次被杀）就接管。
    """
    for _ in range(300):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(LOCK.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid > 0 and _pid_alive(pid):
                print(f"另一个变异脚本在跑（pid {pid}），等着。")
                time.sleep(1.0)
                continue
            # 上一次被杀留下了锁，接管。
            LOCK.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return 0
    print("等不到锁，退出。")
    return 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> int:
    if _acquire_lock() != 0:
        return 2
    try:
        return _run()
    finally:
        LOCK.unlink(missing_ok=True)


def _run() -> int:
    # 别的 pytest 在跑的话，它读到的是半改半没改的源码。
    running = subprocess.run(
        ["pgrep", "-f", "pytest"], capture_output=True, text=True
    ).stdout.strip()
    if running:
        print("还有 pytest 在跑，先停掉：", running)
        return 2

    originals = {
        SOURCE: SOURCE.read_text(encoding="utf-8"),
        MAIN: MAIN.read_text(encoding="utf-8"),
        TRAY: TRAY.read_text(encoding="utf-8"),
    }

    # 基线：不改任何东西，测试必须是绿的，否则下面的结论都不算数。
    code, out = _run_tests()
    print(f"基线 exit={code}")
    if code != 0:
        print(out[-3000:])
        print("基线不绿，停下。先修测试，别谈变异。")
        return 2

    results: list[dict] = []
    for name, path, old, new in MUTATIONS:
        original = originals[path]
        if old not in original:
            print(f"✗ {name}: 片段不在 {path.name} 里——脚本坏了，不是变异存活")
            results.append({"name": name, "caught": False, "script_broken": True})
            continue

        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"✗ {name}: 替换没有生效")
            results.append({"name": name, "caught": False, "script_broken": True})
            continue

        JOURNAL.write_text(
            json.dumps(
                {str(p): c for p, c in originals.items()}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        try:
            path.write_text(mutated, encoding="utf-8")
            code, out = _run_tests()
            caught = code == 1
            if caught:
                print(f"✓ {name}: 抓住了")
            else:
                print(f"✗ {name}: 变异存活（exit={code}）")
                print(out[-1500:])
        finally:
            # 还原必须在这里。
            path.write_text(original, encoding="utf-8")
            JOURNAL.unlink(missing_ok=True)

        results.append({"name": name, "caught": caught, "script_broken": False})

    # 还原之后要再跑一次，确认真的还原了。
    code, out = _run_tests()
    print(f"还原后 exit={code}")
    if code != 0:
        print(out[-3000:])
        return 2

    survived = [r["name"] for r in results if not r["caught"]]
    broken = [r["name"] for r in results if r.get("script_broken")]
    print(f"\n结果：{len(results) - len(survived) - len(broken)}/{len(results)} 抓住")
    if survived:
        print("存活：", survived)
    if broken:
        print("脚本坏了：", broken)
    return 1 if (survived or broken) else 0


if __name__ == "__main__":
    raise SystemExit(main())
