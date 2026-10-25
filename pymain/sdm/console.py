"""控制台输出的编码处理。

Windows 的控制台默认用 locale 编码（简体中文系统是 GBK/cp936）。
程序里只要往 stdout/stderr 写中文，或者写带非 ASCII 字符的路径，
就会抛 UnicodeEncodeError。

崩溃的地方往往很不是时候：自检、CLI 的帮助信息、错误提示。
所以所有会往控制台输出的入口都要先调 `force_utf8_stdio()`。

打包后的 exe 也一样要处理——PyInstaller 不会帮你改编码。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    用 `reconfigure()` 改的是流本身的编码，比设 PYTHONIOENCODING
    环境变量可靠——后者对已经启动的进程无效。

    `errors="replace"` 是兜底：万一某个字符真的编不了（比如在
    只支持 GBK 的老控制台里），输出问号也比整个程序崩掉强。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 被重定向成了二进制流（管道、pytest 捕获等）——不动
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            # 流已关闭或不是 TextIO——忽略
            pass


def safe_print(text: str, *, file=None, end: str = "\n") -> None:
    """打印，编码不兼容时降级而不是崩。

    有些环境（老 Windows 控制台、被重定向到奇怪编码的管道）连
    reconfigure 都救不了。这时候把编不出来的字符替掉。
    """
    target = file if file is not None else sys.stdout
    try:
        print(text, file=target, end=end)
        return
    except UnicodeEncodeError:
        pass
    except ValueError:
        # 流已关闭。打印到已关闭的流上没什么意义，但也不能因此崩。
        return
    except Exception:
        return

    # 编不了：把编不出来的字符替掉再试一次
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        fallback = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(fallback, file=target, end=end)
    except Exception:
        pass
