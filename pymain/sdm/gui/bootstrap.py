"""首次启动时把 CLI 和 daemon 装好。

打包版 .app 里已经带了 CLI（Contents/Resources/sdm-cli/sdm）和
daemon（Contents/Resources/sdmd-daemon/sdmd）。用户拖进
/Applications 之后还差一步：CLI 得进 PATH，daemon 得开机自启。

这一步以前要用户手动跑 install.sh。现在 GUI 启动时自己做。

设计上的几个取舍：

- **不改用户的 shell 配置文件**。往 ~/.zshrc 里塞 PATH 是侵入式
  的行为，用户不一定想要。优先装到 /usr/local/bin（大多数 macOS
  上可写且在 PATH），装不进去才退回 ~/.local/bin，并在界面上提示
  PATH 没配。
- **幂等**。每次启动都检查，发现已经装好且指向当前这个 .app 就
  什么都不做。app 换了路径（重装到别处）才重新指软链。
- **不动已在 PATH 里的同名命令**。如果用户已经有一个别的 `sdm`
  在 PATH 里，不覆盖，报告冲突让他自己决定。
- **daemon 不反复重启**。只在 plist 内容变了才 kickstart，否则
  每次开 GUI 都把正在跑的 daemon 打断一下。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENT_LABEL = "com.skyc8266.superdownloadmanager.daemon"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_daemon_command(daemon_exe: Path) -> str:
    """Windows 计划任务里启动 daemon 的命令行。

    **必须设 `SDM_DAEMON=1`。** daemon 只认这个环境变量才进守护
    模式（Swift 版 Daemon/main.swift:17-26 的守卫），没设就打印
    提示然后 exit 0。计划任务会被标记成「已启动」但进程早就没了，
    用户以为守护进程在跑，其实没有。

    `schtasks` 没有直接设环境变量的参数，只能用 cmd 包一层。

    **引号不能嵌套。** `cmd /c "set X=1&&"C:\\path\\x.exe""` 这种
    写法 cmd 的 /c 解析会提前截断——外层引号包住整条命令时，路径
    自己的引号会被当成结束符。所以外层不加引号，让 cmd 自己按
    `&&` 切分，路径单独引起来。

    cmd 的 `set VAR=VALUE` 里 VALUE 到下一个 `&` 为止，所以
    `set SDM_DAEMON=1&&...` 存进去的值正好是 `1`。
    """
    return f'cmd /c set SDM_DAEMON=1&&"{daemon_exe}"'


def _windows_task_mentions_daemon(task_name: str, daemon_exe: Path) -> bool:
    """查计划任务的 XML，看它是不是在跑我们要的那个 daemon exe。

    schtasks /XML 输出是英文、不被本地化。

    **比的是完整路径，不是文件名。** 只比文件名的话，旧安装目录里
    同名的 `sdmd.exe` 会被当成「已经注册」——计划任务还指着那个早就
    被删掉的路径，daemon 永远起不来。

    判不准的时候宁可当成「不匹配」：那只是重建一次计划任务（daemon
    重启一下）；判成「匹配」而实际不匹配的话，daemon 根本不会启动。

    **用模块级的 `subprocess`**，不要在函数里 `import subprocess`——
    那样会绕开 monkeypatch，测试里替换不掉，测出来的是真机上
    的行为。
    """
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/XML"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            return False
        xml = result.stdout.lower()
        # 路径里的引号在 XML 里可能被转义成 &quot;，去掉再比。
        wanted = str(daemon_exe).lower().strip('"')
        return wanted in xml
    except OSError:
        return False


def bundle_resources() -> Path | None:
    """返回打包版的 Resources 目录；源码运行时返回 None。

    PyInstaller onedir 的 .app 里，可执行文件在 Contents/MacOS/，
    资源在 Contents/Resources/。不靠 sys.frozen 判断——打包成
    onedir 时 sys.executable 指向的是 .app 内部那个二进制，
    源码跑时它指向解释器，两者路径结构完全不同。
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    # .app/Contents/MacOS/xxx → .app/Contents/Resources
    candidate = exe.parent.parent / "Resources"
    if candidate.is_dir() and (candidate / "sdm-cli").is_dir():
        return candidate
    return None


def _pick_cli_dir() -> tuple[Path, bool]:
    """选一个装 CLI 的目录。返回 (目录, 是否在 PATH 里)。

    优先 /usr/local/bin：绝大多数 macOS 上它在 PATH 里且可写
    （Homebrew 用户、装过开发工具的用户）。装不进去才用
    ~/.local/bin——那个目录在 PATH 里的机器不多，所以第二个
    返回值是 False，界面上要提示用户自己配 PATH。
    """
    home = Path.home()
    local = home / ".local" / "bin"
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    for candidate in (Path("/usr/local/bin"), local):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".sdm-write-probe"
            probe.touch()
            probe.unlink()
        except OSError:
            continue
        return candidate, str(candidate) in path_dirs
    return local, str(local) in path_dirs


def _existing_command(name: str) -> str | None:
    """PATH 里已有的同名命令路径（排除我们要装的那个位置）。"""
    return shutil.which(name)


def install_cli(resources: Path) -> dict:
    """把 CLI 软链到 PATH 目录。返回状态字典。"""
    cli_bin = resources / "sdm-cli" / "sdm"
    if not cli_bin.is_file():
        return {"ok": False, "reason": "bundle_missing_cli", "path": ""}

    target_dir, in_path = _pick_cli_dir()
    link = target_dir / "sdm"

    existing = _existing_command("sdm")
    if existing and existing != str(link):
        # PATH 里已经有一个别的 sdm。不动它——可能是用户自己装的
        # 别的工具，悄悄覆盖会让人debug到怀疑人生。
        return {
            "ok": False,
            "reason": "conflict",
            "path": existing,
            "in_path": True,
        }

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            # 比 resolve 后的路径，不比原始字符串。
            #
            # Windows 上 os.readlink 返回的是带 \\?\ 前缀的规范化路径
            # （\\?\C:\...），跟建的时候用的字符串永远不相等——直接比
            # 字符串的话幂等判断在 Windows 上恒为假，每次启动都重装。
            # macOS 上 /tmp 之类的路径也可能有 symlink 差异。
            try:
                same = link.resolve() == cli_bin.resolve()
            except OSError:
                same = False
            if same:
                return {"ok": True, "reason": "already", "path": str(link), "in_path": in_path}
            link.unlink()
        elif link.exists():
            # 那个位置已经有个普通文件——可能是用户自己放的同名工具。
            # 不删它，报告冲突让用户自己决定。
            return {
                "ok": False,
                "reason": "conflict",
                "path": str(link),
                "in_path": in_path,
            }
        link.symlink_to(cli_bin)
    except OSError as err:
        return {"ok": False, "reason": f"symlink_failed: {err}", "path": str(link), "in_path": in_path}

    return {"ok": True, "reason": "installed", "path": str(link), "in_path": in_path}


def _launch_agent_plist(daemon_bin: Path, agents_dir: Path | None = None) -> Path:
    """写 LaunchAgent 的 plist，返回它的路径。

    `agents_dir` 默认是用户真实的 `~/Library/LaunchAgents`。
    **测试必须传临时目录**——写用户真实的目录会留下一个
    `com.test.sdm.daemon` 之类的垃圾 plist，还会被 launchd 认走
    （KeepAlive=true 的坏 job 会每 10 秒崩一次重启）。
    """
    agents = agents_dir or (Path.home() / "Library" / "LaunchAgents")
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / f"{LAUNCH_AGENT_LABEL}.plist"
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(daemon_bin)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        # daemon 只认 SDM_DAEMON=1，不设这个它会打印提示然后退出 0。
        # launchd 会因为进程立刻退出而反复重启它——日志里全是崩溃。
        "EnvironmentVariables": {"SDM_DAEMON": "1"},
        "StandardOutPath": f"/tmp/{LAUNCH_AGENT_LABEL}.out.log",
        "StandardErrorPath": f"/tmp/{LAUNCH_AGENT_LABEL}.err.log",
    }
    target.write_bytes(plistlib.dumps(payload))
    return target


def register_daemon(resources: Path, agents_dir: Path | None = None) -> dict:
    """注册并加载 daemon 的 LaunchAgent。返回状态字典。

    `agents_dir` 传临时目录时**不碰 launchd**——只写 plist。
    测试用这个，别去动用户真实的 launchd 域。
    """
    daemon_bin = resources / "sdmd-daemon" / "sdmd"
    if not daemon_bin.is_file():
        return {"ok": False, "reason": "bundle_missing_daemon", "path": ""}

    target = _launch_agent_plist(daemon_bin, agents_dir)
    if agents_dir is not None:
        # 测试路径：只写 plist，不 bootstrap 到真实 launchd 域。
        return {"ok": True, "reason": "plist_written", "path": str(target)}

    uid = os.getuid()
    domain = f"gui/{uid}"

    # launchctl 的 target 必须是 gui/<uid>/<label>。只传 label 会报
    # "Unrecognized target specifier"（exit 64）。
    #
    # **只看「有没有加载」不够——还得看加载的是哪一份 plist。**
    # launchd 是按 label 认 job 的：同名 job 一旦加载着，
    # `launchctl print` 就成功，哪怕它指向的是别的路径（比如
    # 一个已经被删掉的临时目录）。这时候直接返回「已加载」，
    # daemon 就永远注册不上，launchd 每 10 秒崩一次重启。
    probe = subprocess.run(
        ["launchctl", "print", f"{domain}/{LAUNCH_AGENT_LABEL}"],
        capture_output=True,
        text=True,
    )
    loaded = probe.returncode == 0
    # launchd 打印的 plist 路径。跟我们要注册的那份不一致 = 得重装。
    stale = loaded and str(target) not in probe.stdout

    # plist 变了才重启 daemon。内容没变时 launchctl bootstrap 会
    # 报 "service already loaded"，而 kickstart 会把正在跑的
    # daemon 打断一下。
    if loaded and not stale:
        return {"ok": True, "reason": "already_loaded", "path": str(target)}

    # 加载着别的一份（路径不对）——先把它踢掉，不然 bootstrap 会
    # 报 "service already loaded"。
    if loaded:
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"],
            capture_output=True,
        )

    subprocess.run(
        ["launchctl", "bootout", domain, str(target)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "reason": f"bootstrap_failed: {result.stderr.strip()[:200]}",
            "path": str(target),
        }
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}"],
        capture_output=True,
    )
    return {"ok": True, "reason": "reloaded" if stale else "loaded", "path": str(target)}


def _windows_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "SuperDownloadManager"


def install_native_host(resources: Path) -> dict:
    """把浏览器原生消息宿主装上（规格 §3.1）。

    浏览器按「原生消息宿主清单」里的 `path` 拉起宿主进程。清单要
    写到各个浏览器的 NativeMessagingHosts 目录里——那是**用户级**
    目录，不需要管理员权限。

    装不上不影响下载功能（扩展还有回环 HTTP 那条通道），所以失败
    只返回状态，不抛。
    """
    host_bin = resources / "native-host" / "sdmd-native-host"
    if not host_bin.is_file():
        return {"ok": False, "reason": "bundle_missing_host", "path": ""}
    try:
        from ..ipc.native_host import install_native_host as install

        written = install(host_bin)
        return {"ok": True, "reason": "installed", "path": str(written[0]) if written else ""}
    except Exception as err:
        return {"ok": False, "reason": f"install_failed: {err}", "path": ""}


def install_native_host_windows(executable: Path) -> dict:
    """Windows 版本：宿主 exe 已经解出来了，装清单即可。"""
    if not executable.is_file():
        return {"ok": False, "reason": "host_missing", "path": ""}
    try:
        from ..ipc.native_host import install_native_host as install

        written = install(executable)
        return {"ok": True, "reason": "installed", "path": str(written[0]) if written else ""}
    except Exception as err:
        return {"ok": False, "reason": f"install_failed: {err}", "path": ""}


def _extract_zip_once(archive: Path, dest: Path) -> bool:
    """把 zip 解到 dest。已解过（dest 里有标记文件）就跳过。"""
    marker = dest / ".extracted"
    if marker.exists():
        return True
    import zipfile

    if not archive.is_file():
        return False
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    except (OSError, zipfile.BadZipFile):
        shutil.rmtree(dest, ignore_errors=True)
        return False
    marker.write_text("ok", encoding="utf-8")
    return True


def _ensure_windows() -> dict:
    """Windows 首次运行：释放 CLI/daemon，装 PATH，注册计划任务。

    GUI 是 onefile 单文件，装不下第二份 Python 运行时，所以 CLI 和
    daemon 以 zip 形式塞在 GUI 里，首次运行时解到
    %LOCALAPPDATA%\\SuperDownloadManager\\ 下——解出来的还是 onedir，
    启动快（onefile 每次启动要解包，慢十几秒）。
    """
    result = {
        "platform": "win32",
        "cli": {"ok": False, "reason": "not_bundle", "path": "", "in_path": False},
        "daemon": {"ok": False, "reason": "not_bundle", "path": ""},
        "native_host": {"ok": False, "reason": "not_bundle", "path": ""},
        "url_scheme": {"ok": False, "reason": "not_bundle"},
    }
    if not getattr(sys, "frozen", False):
        return result
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return result

    base = _windows_appdata()
    runtime = base / "runtime"
    cli_zip = Path(meipass) / "sdm-cli.zip"
    daemon_zip = Path(meipass) / "sdmd-daemon.zip"
    host_zip = Path(meipass) / "sdmd-native-host.zip"

    if not _extract_zip_once(cli_zip, runtime / "sdm"):
        return result
    if not _extract_zip_once(daemon_zip, runtime / "sdmd"):
        return result
    # 原生消息宿主（规格 §3.1）。浏览器按清单里的 path 拉起它，
    # 解不出来就装不了清单——但这不影响下载功能，所以失败只记
    # 下来，不中断启动。
    host_exe: Path | None = None
    if _extract_zip_once(host_zip, runtime / "sdmd-native-host"):
        candidate = runtime / "sdmd-native-host" / "sdmd-native-host.exe"
        if candidate.is_file():
            host_exe = candidate
    result["native_host"] = {"ok": host_exe is not None, "path": str(host_exe or "")}
    if host_exe is not None:
        # 装清单（浏览器按清单里的 path 拉起宿主）。
        installed = install_native_host_windows(host_exe)
        result["native_host"] = installed

    cli_exe = runtime / "sdm" / "sdm.exe"
    daemon_exe = runtime / "sdmd" / "sdmd.exe"

    # .cmd shim 放到 PATH 上——Windows 没软链，shim 是最稳的
    shim_dir = base / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "sdm.cmd"
    # 相对路径：%~dp0..\runtime\sdm\sdm.exe
    # %~dp0 = shim 所在目录（以 \ 结尾），后面接相对路径，全程纯 ASCII
    rel = r"..\runtime\sdm\sdm.exe"
    try:
        shim.write_text(
            '@echo off\r\nREM SDM CLI shim\r\n'
            '"%~dp0' + rel + '" %*\r\n',
            encoding="ascii",
        )
    except OSError as err:
        result["cli"] = {"ok": False, "reason": f"shim_failed: {err}", "path": "", "in_path": False}
        return result

    # CLI 可执行文件没解出来的话，shim 指向的是空气——写出来也没用。
    # 不静默成功：用户会以为命令行能用了，跑起来才报「不是有效的
    # Win32 应用程序」。
    if not cli_exe.is_file():
        result["cli"] = {
            "ok": False,
            "reason": "cli_exe_missing",
            "path": str(cli_exe),
            "in_path": False,
        }
        return result

    # 把 shim 目录加进用户 PATH。用 winreg 而不是 setx——setx 有
    # 1024 字符截断，老机器的 PATH 经常超。
    #
    # **ImportError 也要接。** winreg 是标准库但只在 Windows 上有；
    # 这里的代码路径理论上只在 Windows 上跑，但打包后的测试、
    # 交叉环境都可能碰到。接住它，PATH 没配上只影响命令行好不好用，
    # 不该让 GUI 起不来。
    #
    # **失败不能提前 return。** PATH 没配上只是命令行要手敲全路径，
    # 后面的 daemon 计划任务注册跟它无关——之前这里 return 掉了，
    # 用户的 daemon 永远注册不上，命令行和后台下载两个都废了。
    path_ok = True
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            raw, _ = winreg.QueryValueEx(key, "Path")
            parts = [p for p in (raw or "").split(";") if p]
            if str(shim_dir).lower() not in [p.lower() for p in parts]:
                parts.append(str(shim_dir))
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(parts))
        finally:
            winreg.CloseKey(key)
    except (OSError, ImportError) as err:
        path_ok = False
        result["cli"] = {
            "ok": True,
            "reason": f"shim_ok_path_failed: {err}",
            "path": str(shim),
            "in_path": False,
        }

    if path_ok:
        result["cli"] = {"ok": True, "reason": "installed", "path": str(shim), "in_path": True}

    # daemon 计划任务：幂等注册，不杀正在跑的 daemon
    if daemon_exe.is_file():
        task = "SuperDownloadManagerDaemon"
        try:
            # 先查现有任务：如果存在且指向同一个 exe，就别动它
            if _windows_task_mentions_daemon(task, daemon_exe):
                # 任务已存在且正确，只确保它在跑
                subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True)
                result["daemon"] = {"ok": True, "reason": "task_already_registered", "path": task}
            else:
                # 任务不存在或指向旧 exe —— 删除旧的（如果有），创建新的
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", task, "/F"],
                    capture_output=True,
                )
                subprocess.run(
                    [
                        "schtasks", "/Create",
                        "/TN", task,
                        "/TR", _windows_daemon_command(daemon_exe),
                        "/SC", "ONLOGON",
                        "/RL", "LIMITED",
                        "/F",
                    ],
                    capture_output=True,
                )
                subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True)
                result["daemon"] = {"ok": True, "reason": "task_registered", "path": task}
        except OSError as err:
            result["daemon"] = {"ok": False, "reason": f"task_failed: {err}", "path": task}

    # URL Scheme 注册（规格 §4.2 的路径 A / §4.5）。
    #
    # **幂等**：`register_url_scheme()` 自己会先比对现状，一样就
    # 什么都不写。注册表写入会通知 Explorer 刷新，每次启动都重写
    # 一遍是白折腾。
    #
    # 写在 HKCU 下，普通用户就能注册，不需要管理员权限。
    try:
        from ..system_integration import register_url_scheme

        ok = register_url_scheme()
        result["url_scheme"] = {"ok": ok, "reason": "registered" if ok else "skipped"}
    except Exception as err:
        result["url_scheme"] = {"ok": False, "reason": f"url_scheme_failed: {err}"}
    return result


def ensure_installed() -> dict:
    """装 CLI + daemon。返回整体状态。

    源码运行时什么都不做——开发的时候不该往用户的 /usr/local/bin
    里塞软链。
    """
    result = {
        "platform": sys.platform,
        "cli": {"ok": False, "reason": "not_bundle", "path": "", "in_path": False},
        "daemon": {"ok": False, "reason": "not_bundle", "path": ""},
        "native_host": {"ok": False, "reason": "not_bundle", "path": ""},
    }
    resources = bundle_resources()
    if resources is None:
        return result

    if _is_macos():
        result["cli"] = install_cli(resources)
        result["daemon"] = register_daemon(resources)
        # 浏览器原生消息宿主（规格 §3.1）。装不上不影响下载——
        # 扩展还有回环 HTTP 那条通道，只是多一次手动配对。
        result["native_host"] = install_native_host(resources)
    elif _is_windows():
        result = _ensure_windows()
    return result
