#!/usr/bin/env python3
"""单实例本地消息转交和 GitHub 正式版本检查；仅用标准库。"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_VERSION = "1.3.0"
REPO = "baijiamingxa/files-tools"
RELEASES_URL = f"https://github.com/{REPO}/releases"


def user_directory():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "文件工具箱"


class InstanceError(Exception):
    pass


def _receive(connection):
    data = bytearray()
    while b"\n" not in data:
        part = connection.recv(4096)
        if not part:
            raise InstanceError("本地窗口连接已关闭。")
        data.extend(part)
        if len(data) > 65536:
            raise InstanceError("传入文件列表过长，请分批打开。")
    return json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))


class SingleInstance:
    """文件锁决定唯一主实例，随机令牌保护 loopback 消息端口。"""
    def __init__(self, directory=None):
        self.directory = Path(directory or user_directory())
        self.directory.mkdir(parents=True, exist_ok=True)
        self.endpoint = self.directory / "窗口连接.json"
        self.lock = open(self.directory / "窗口.lock", "a+b")
        self.lock.seek(0, 2)
        if not self.lock.tell():
            self.lock.write(b"0")
            self.lock.flush()
        self.primary = False
        self.listener = self.thread = None
        self.stop = threading.Event()
        self.messages = queue.Queue(maxsize=100)
        try:
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.primary = True
        except OSError:
            return
        try:
            self.token = secrets.token_hex(32)
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(8)
            self.listener.settimeout(0.2)
            metadata = {"port": self.listener.getsockname()[1], "token": self.token, "pid": os.getpid()}
            temp = self.endpoint.with_name(self.endpoint.name + "." + self.token[:12] + ".tmp")
            try:
                temp.write_text(json.dumps(metadata), encoding="utf-8")
                if os.name != "nt":
                    temp.chmod(0o600)
                os.replace(temp, self.endpoint)
            finally:
                temp.unlink(missing_ok=True)
            self.thread = threading.Thread(target=self._listen, daemon=True, name="工具箱窗口通信")
            self.thread.start()
        except Exception:
            self.close()
            raise

    def _listen(self):
        listener = self.listener
        while not self.stop.is_set():
            try:
                connection, _ = listener.accept()
            except (OSError, socket.timeout):
                continue
            with connection:
                connection.settimeout(1)
                try:
                    data = _receive(connection)
                    token, inputs = data.get("token", ""), data.get("inputs", [])
                    valid = (isinstance(token, str) and secrets.compare_digest(token, self.token)
                             and isinstance(inputs, list) and len(inputs) <= 500
                             and all(isinstance(value, str) and "\x00" not in value for value in inputs))
                    if not valid or self.stop.is_set():
                        raise InstanceError("无效的窗口消息。")
                    self.messages.put_nowait(inputs)
                    connection.sendall(b'{"ok":true}\n')
                except (ValueError, TypeError, AttributeError, OSError, InstanceError, queue.Full):
                    try:
                        connection.sendall(b'{"ok":false}\n')
                    except OSError:
                        pass

    def forward(self, inputs, timeout=8):
        deadline = time.monotonic() + timeout
        error = ""
        while time.monotonic() < deadline:
            try:
                metadata = json.loads(self.endpoint.read_text(encoding="utf-8"))
                if os.name == "nt":
                    import ctypes
                    ctypes.windll.user32.AllowSetForegroundWindow(int(metadata["pid"]))
                payload = json.dumps({"token": metadata["token"], "inputs": list(inputs)}, ensure_ascii=False).encode("utf-8") + b"\n"
                if len(payload) > 65536:
                    raise InstanceError("传入文件列表过长，请分批打开。")
                port = int(metadata["port"])
                if not 1 <= port <= 65535:
                    raise ValueError("本地端口无效")
                with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
                    connection.settimeout(2)
                    connection.sendall(payload)
                    if _receive(connection).get("ok"):
                        return
                    raise InstanceError("已开窗口暂时无法接受文件，请稍后重试。")
            except InstanceError:
                raise
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                error = str(exc)
                time.sleep(0.1)
        raise InstanceError(f"已有工具箱窗口正在启动或退出，请稍后重试。\n{error}")

    def close(self):
        self.stop.set()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.primary and not self.lock.closed:
            try:
                if self.endpoint.is_file():
                    data = json.loads(self.endpoint.read_text(encoding="utf-8"))
                    if data.get("token") == getattr(self, "token", None):
                        self.endpoint.unlink()
            except (OSError, ValueError):
                pass
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()


def focus_window(app):
    app.deiconify()
    app.lift()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        handle = user.GetAncestor(app.winfo_id(), 2)
        user.ShowWindow(handle, 9)
        if not user.SetForegroundWindow(handle):
            user.FlashWindow(handle, True)
    else:
        app.focus_force()


def version_tuple(tag):
    match = re.fullmatch(r"(?:(?:txt-encoder|txt编码器|文件工具箱)[-_]?)?v?(\d+)\.(\d+)\.(\d+)", str(tag), re.IGNORECASE)
    return tuple(int(part) for part in match.groups()) if match else None


def check_release(current=APP_VERSION, opener=None):
    request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest", headers={"User-Agent": f"FileToolbox/{APP_VERSION}", "Accept": "application/vnd.github+json"})
    # API 可直连；不带令牌，也不写死用户的代理端口。
    request_open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with request_open(request, timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("版本信息体积异常。")
            data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"new": False, "version": "", "url": RELEASES_URL, "notes": "仓库尚未发布正式版本。"}
        raise
    version = version_tuple(data.get("tag_name"))
    if not version or data.get("draft") or data.get("prerelease"):
        raise ValueError("未找到有效的正式版本信息。")
    url = data.get("html_url", "")
    if not url.startswith(RELEASES_URL + "/tag/"):
        raise ValueError("版本页面不是当前仓库的正式发布地址。")
    return {"new": version > version_tuple(current), "version": data["tag_name"], "url": url, "notes": str(data.get("body") or "暂无版本说明。")[:20000]}
