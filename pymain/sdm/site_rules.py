"""站点规则：按主机名附加请求头与 Basic Auth。

对应 Swift 版 Engine/SiteRule.swift。

密码不写进 JSON——进系统钥匙串（macOS Keychain / Windows
Credential Locker）。JSON 里只留用户名，密码按 `用户@主机` 索引取。
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from . import paths
from .logging_setup import CAT_STORE, get_logger

log = get_logger()


@dataclass
class SiteRule:
    """一个主机的附加请求配置。"""

    id: str
    host: str
    referer: str | None = None
    user_agent: str | None = None
    cookies: str | None = None
    basic_auth_user: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SiteRule:
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            host=data.get("host", ""),
            referer=data.get("referer") or None,
            user_agent=data.get("user_agent") or None,
            cookies=data.get("cookies") or None,
            basic_auth_user=data.get("basic_auth_user") or None,
        )


class Keychain:
    """系统钥匙串的薄封装。

    macOS 走 `security` 命令行（系统自带，不需要额外依赖）。
    Windows 走 DPAPI（用 ctypes 调 crypt32.dll）。
    两者都拿不到时退回一个进程内的内存存储——功能不丢，
    只是重启后密码不保留，日志里会记一条警告。
    """

    SERVICE = "com.skyc8266.superdownloadmanager.siteRules"

    def __init__(self) -> None:
        self._fallback: dict[str, str] = {}
        self._lock = threading.Lock()
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        import shutil
        import sys

        if sys.platform == "darwin" and shutil.which("security"):
            return "macos"
        if sys.platform.startswith("win"):
            return "dpapi"
        return "memory"

    def _account_key(self, account: str, host: str) -> str:
        return f"{account}@{host}"

    def set_password(self, account: str, host: str, password: str) -> bool:
        key = self._account_key(account, host)
        if self._backend == "macos":
            import subprocess

            try:
                subprocess.run(
                    [
                        "security", "add-generic-password",
                        "-U",  # 覆盖已存在的
                        "-s", self.SERVICE,
                        "-a", key,
                        "-w", password,
                    ],
                    check=True,
                    capture_output=True,
                )
                return True
            except Exception as err:
                log.warning(CAT_STORE, f"钥匙串写入失败（改用内存存储）: {err}")
                self._backend = "memory"
        elif self._backend == "dpapi":
            if self._dpapi_set(key, password):
                return True
            self._backend = "memory"

        with self._lock:
            self._fallback[key] = password
        return False

    def get_password(self, account: str, host: str) -> str | None:
        key = self._account_key(account, host)
        if self._backend == "macos":
            import subprocess

            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", self.SERVICE, "-a", key, "-w"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip() or None
            except Exception:
                return None
        if self._backend == "dpapi":
            value = self._dpapi_get(key)
            if value is not None:
                return value
        with self._lock:
            return self._fallback.get(key)

    def remove_password(self, account: str, host: str) -> None:
        key = self._account_key(account, host)
        if self._backend == "macos":
            import subprocess

            try:
                subprocess.run(
                    ["security", "delete-generic-password", "-s", self.SERVICE, "-a", key],
                    check=False,
                    capture_output=True,
                )
            except Exception:
                pass
        elif self._backend == "dpapi":
            self._dpapi_remove(key)
        with self._lock:
            self._fallback.pop(key, None)

    # -- Windows DPAPI ------------------------------------------------

    @staticmethod
    def _dpapi_set(key: str, password: str) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                # ClassVar 是给 ruff 看的：`_fields_` 是个可变列表，
                # 不标注的话它会当成「所有实例共享的类属性」。
                # ctypes 只认名字不认注解，运行时没有任何影响。
                _fields_: ClassVar = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char)),
                ]

            windll = ctypes.windll
            crypt32 = windll.crypt32
            kernel32 = windll.kernel32

            raw = password.encode("utf-16-le")
            in_blob = DATA_BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_char)))
            out_blob = DATA_BLOB()

            if not crypt32.CryptProtectData(
                ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
            ):
                return False
            try:
                encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                store = Path(paths.config_dir()) / "site_rules.keyring"
                store.parent.mkdir(parents=True, exist_ok=True)
                import json as _json

                data = _json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
                data[key] = base64.b64encode(encrypted).decode("ascii")
                store.write_text(_json.dumps(data, indent=2), encoding="utf-8")
                return True
            finally:
                kernel32.LocalFree(out_blob.pbData)
        except Exception:
            return False

    @staticmethod
    def _dpapi_get(key: str) -> str | None:
        try:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                # ClassVar 是给 ruff 看的：`_fields_` 是个可变列表，
                # 不标注的话它会当成「所有实例共享的类属性」。
                # ctypes 只认名字不认注解，运行时没有任何影响。
                _fields_: ClassVar = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char)),
                ]

            store = Path(paths.config_dir()) / "site_rules.keyring"
            if not store.exists():
                return None
            import json as _json

            data = _json.loads(store.read_text(encoding="utf-8"))
            if key not in data:
                return None
            encrypted = base64.b64decode(data[key])

            windll = ctypes.windll
            crypt32 = windll.crypt32
            kernel32 = windll.kernel32

            in_blob = DATA_BLOB(
                len(encrypted),
                ctypes.cast(ctypes.create_string_buffer(encrypted), ctypes.POINTER(ctypes.c_char)),
            )
            out_blob = DATA_BLOB()
            if not crypt32.CryptUnprotectData(
                ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
            ):
                return None
            try:
                decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                return decrypted.decode("utf-16-le")
            finally:
                kernel32.LocalFree(out_blob.pbData)
        except Exception:
            return None

    @staticmethod
    def _dpapi_remove(key: str) -> None:
        store = Path(paths.config_dir()) / "site_rules.keyring"
        if not store.exists():
            return
        import json as _json

        try:
            data = _json.loads(store.read_text(encoding="utf-8"))
            data.pop(key, None)
            store.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass


class SiteRuleStore:
    """规则列表的读写。"""

    _instance: SiteRuleStore | None = None
    _instance_lock = threading.Lock()

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.site_rules_path()
        self._keychain = Keychain()
        self._lock = threading.Lock()

    @classmethod
    def shared(cls) -> SiteRuleStore:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -- 读写 --------------------------------------------------------

    def load(self) -> list[SiteRule]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [SiteRule.from_dict(item) for item in data if isinstance(item, dict)]

    def save(self, rules: list[SiteRule]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [rule.to_dict() for rule in rules]
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)

    # -- 增删 --------------------------------------------------------

    def add(self, rule: SiteRule, password: str | None = None) -> None:
        rules = self.load()
        rules.append(rule)
        self.save(rules)
        if password and rule.basic_auth_user:
            self._keychain.set_password(rule.basic_auth_user, rule.host, password)

    def remove(self, rule_id: str) -> None:
        rules = self.load()
        target = next((r for r in rules if r.id == rule_id), None)
        self.save([r for r in rules if r.id != rule_id])
        if target and target.basic_auth_user:
            self._keychain.remove_password(target.basic_auth_user, target.host)

    def clear_all(self) -> None:
        for rule in self.load():
            if rule.basic_auth_user:
                self._keychain.remove_password(rule.basic_auth_user, rule.host)
        self.save([])

    # -- 匹配 --------------------------------------------------------

    def match(self, url: str) -> SiteRule | None:
        """按主机名匹配。后缀匹配——`cdn.example.com` 能命中 `example.com` 的规则。"""
        from urllib.parse import urlparse

        host = urlparse(url).hostname
        if not host:
            return None
        host = host.lower()
        for rule in self.load():
            rule_host = rule.host.lower()
            if host == rule_host or host.endswith("." + rule_host):
                return rule
        return None

    def get_password(self, account: str, host: str) -> str | None:
        return self._keychain.get_password(account, host)

    def apply_to_headers(self, rule: SiteRule, headers: dict[str, str]) -> dict[str, str]:
        """把规则里的请求头叠加到 headers 上。"""
        if rule.referer:
            headers["Referer"] = rule.referer
        if rule.user_agent:
            headers["User-Agent"] = rule.user_agent
        if rule.cookies:
            headers["Cookie"] = rule.cookies
        if rule.basic_auth_user:
            password = self.get_password(rule.basic_auth_user, rule.host)
            if password:
                raw = f"{rule.basic_auth_user}:{password}".encode()
                token = base64.b64encode(raw).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
        return headers
