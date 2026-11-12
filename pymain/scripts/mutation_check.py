"""变异测试：确认调度器的测试真的在测东西。

**绿色的测试可能什么都没测。** 2026-10-25 踩过：节流测试只断言
「等过」，而通用节流自己就满足了这个断言——把写队列整个拆掉，
测试还是绿的。

这里把实现改坏一遍，看测试会不会红。每一处变异都必须被抓住；
抓不住就说明那条测试是假的。

用法：
    python scripts/mutation_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (文件, 原文, 变异后的文本, 说明)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "sdm/engine/download_flow.py",
        "                    await self._budget.acquire()\n",
        "                    pass  # 变异：不占全局预算\n",
        "worker 不占全局预算名额",
    ),
    (
        "sdm/engine/download_flow.py",
        "                        self._budget.release()\n",
        "                        pass  # 变异：不归还预算名额\n",
        "worker 不归还全局预算名额",
    ),
    (
        "sdm/engine/task_queue.py",
        "        return max(MIN_SLOTS_PER_TASK, share)\n",
        "        return share  # 变异：去掉每任务最低份额\n",
        "任务份额下限被去掉（任务饿死）",
    ),
    (
        "sdm/engine/task_queue.py",
        "            self._per_task.pop(task_id, None)\n",
        "            pass  # 变异：任务结束不归还份额\n",
        "任务结束不归还份额",
    ),
    (
        "sdm/engine/download_engine.py",
        "            self._rebalance_slots()\n",
        "            pass  # 变异：不重新分配份额\n",
        "新任务进来时不重新分配份额",
    ),
    (
        "sdm/engine/download_flow.py",
        "        current = self._governor.limit\n        if current > 1:\n            self._governor.set_limit(max(1, current // 2))\n",
        "        current = self._governor.limit\n",
        "错误之后连接数不减半",
    ),
    (
        "sdm/engine/download_flow.py",
        "        self._error_backoff_until = now + 5.0\n",
        "        self._error_backoff_until = now  # 变异：没有冷却期\n",
        "错误之后没有冷却期",
    ),
    (
        "sdm/engine/download_flow.py",
        "                    self._governor.set_limit(\n"
        "                        min(effective_max, max(1, self._governor.limit) * 2)\n"
        "                    )\n",
        "                    self._governor.set_limit(self._governor.limit + 1)\n",
        "连接爬坡退回每条 +1（32 线程要等 3 秒）",
    ),
    (
        "sdm/engine/download_flow.py",
        "                if self._governor.limit < effective_max:\n"
        "                    # 翻倍而不是加一：见上面 docstring。加一让 32 个\n"
        "                    # 线程等 3 秒才用上，用户看到的是「有线程一直排队」。\n"
        "                    self._governor.set_limit(\n"
        "                        min(effective_max, max(1, self._governor.limit) * 2)\n"
        "                    )\n",
        "                if self._governor.limit < effective_max:\n"
        "                    self._governor.set_limit(effective_max)\n",
        "连接爬坡一步到位（连接风暴）",
    ),
    (
        "sdm/engine/download_flow.py",
        "                if self._last_progress_at < time.monotonic() - 2.0:\n"
        "                    continue\n",
        "                if False:\n"
        "                    continue  # 变异：没有进度也加连接\n",
        "卡住的时候还继续加连接",
    ),
    (
        "sdm/engine/download_flow.py",
        "                if time.monotonic() < self._error_backoff_until:\n"
        "                    continue\n",
        "                if False:\n"
        "                    continue  # 变异：冷却期内也加连接\n",
        "错误冷却期内还继续加连接",
    ),
    (
        "sdm/engine/download_flow.py",
        "                effective_max = min(self.parallelism_cap, self._slot_limit)\n",
        "                effective_max = self.parallelism_cap  # 变异：忽略份额\n",
        "连接数越过任务份额上限",
    ),
]


def _run_tests() -> tuple[bool, str]:
    """跑调度器相关的测试。返回 (是否通过, 输出尾部)。"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_shared_budget.py",
            "tests/test_scheduler_handoff.py",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = (proc.stdout + proc.stderr)[-3000:]
    return proc.returncode == 0, output


def _apply(path: Path, old: str, new: str) -> bool:
    """把文件里的 old 换成 new。返回是否成功。"""
    text = path.read_text(encoding="utf-8")
    if old not in text:
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


def _failing_tests(output: str) -> str:
    """从 pytest 输出里挑出失败的用例名。

    变异测试的产出是「哪条变异被哪个测试抓住」——只说「抓住了」，
    还得自己去翻日志才知道是哪条测试在守它。
    """
    names = re.findall(r"^FAILED (\S+)", output, flags=re.MULTILINE)
    if not names:
        return ""
    short = [n.split("::")[-1] for n in names[:3]]
    return "、".join(short)


def _find_leftover_mutations() -> list[tuple[str, int, str]]:
    """扫源码里有没有上一轮留下的变异。

    **变异必须被当场还原。** 进程被杀掉的时候还原代码也执行不到，
    变异的实现就留在仓库里了——下次跑测试直接是红的，而且没人
    知道是被变异过的（那行代码看起来完全合理，只是少了一句
    `self._budget.release()`）。

    认得出来的标记是每条变异里的 `变异：` 注释。
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted((ROOT / "sdm").rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            if "变异：" in line:
                found.append((str(path.relative_to(ROOT)), i, line))
    return found


def main() -> int:
    # **先确认树是干净的。** 上一轮变异跑一半被杀掉的话，变异的代码
    # 会留在仓库里——脚本自己还原不了（进程都没了）。这一段扫出来
    # 直接报错，而不是拿一个被污染的基线去跑变异。
    dirty = _find_leftover_mutations()
    if dirty:
        print("源码里还留着上一轮的变异，先还原再跑：")
        for path_rel, line_no, text in dirty:
            print(f"  {path_rel}:{line_no}: {text.strip()}")
        return 2

    baseline_ok, baseline_out = _run_tests()
    if not baseline_ok:
        print("基线就是红的，先修基线再谈变异：")
        print(baseline_out)
        return 1
    print("基线绿。开始变异……")

    escaped: list[tuple[str, str, str, str]] = []
    for path_rel, old, new, desc in MUTATIONS:
        path = ROOT / path_rel
        original = path.read_text(encoding="utf-8")
        try:
            if not _apply(path, old, new):
                print(f"  SKIP  {desc}（没找到要改的代码——实现变了？）")
                escaped.append((path_rel, old, new, desc))
                continue
            ok, out = _run_tests()
        finally:
            # **必须还原。** 之前这个还原写在 try 之外，中途被
            # Ctrl-C 或者超时杀掉的话，**变异的代码就留在仓库里了**——
            # 下次跑测试直接是红的，而且没人知道是被变异过的。
            path.write_text(original, encoding="utf-8")
        if ok:
            print(f"  漏网  {desc}")
            escaped.append((path_rel, old, new, desc))
        else:
            # 报一下是哪个测试抓住的——变异测试失败时要知道往哪看。
            caught = _failing_tests(out)
            suffix = f"（{caught}）" if caught else ""
            print(f"  抓住  {desc}{suffix}")

    if escaped:
        print()
        print(f"有 {len(escaped)} 处变异没被抓住——这些测试是假的：")
        for _, _, _, desc in escaped:
            print(f"  - {desc}")
        return 1
    print()
    print(f"全部 {len(MUTATIONS)} 处变异都被抓住。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
