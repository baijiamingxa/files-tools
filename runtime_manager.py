#!/usr/bin/env python3
"""用户目录中的便携扩展引擎；检测、校验、下载均仅使用标准库。"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
import zipfile
from pathlib import Path

SEVEN_VERSION = "26.05"
ZSTD_VERSION = "1.5.7"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# 固定版本与校验值，不能用下载时返回的任意校验值替代。
SEVEN_ASSETS = {
    "bootstrap": ("7zr.exe", "9bf582b894dfa665f852d7e9112b02290a1c303c2d95a21fcdc41ffcb03143b7"),
    "x64": ("7z2605-x64.exe", "fd99e86f57d8f429ef5ed045bf98315af962e2309b0f1ba87a147a44c71647d0"),
    "x86": ("7z2605.exe", "6435a470fe7c4769962dbed8a60c641ee21401c833ced06077c80e33e7845a5e"),
    "arm64": ("7z2605-arm64.exe", "86d5790d5dd84cab4636c3bb7fab10d3ed7cd3e57e6119401bd2769e28165268"),
}
ZSTD_ASSETS = {
    "x64": ("zstd-v1.5.7-win64.zip", "acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9"),
    "x86": ("zstd-v1.5.7-win32.zip", "231265caed189d933339cda2352e0905ae9076b43c534ba24f84fbab8f719a2d"),
}
_tools = {}


class RuntimeErrorBase(Exception):
    pass


class RuntimeCancelled(RuntimeErrorBase):
    pass


def architecture():
    machine = (os.environ.get("PROCESSOR_ARCHITEW6432") or platform.machine()).lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    raise RuntimeErrorBase(f"暂不支持自动配置此处理器：{machine}")


def runtime_root():
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / ".local" / "share") / "文件工具箱" / "运行组件"


def check_cancel(stop):
    if stop is not None and stop.is_set():
        raise RuntimeCancelled("已取消组件配置，可稍后重试。")


def digest_file(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run_process(command, stop=None, timeout=60, cwd=None, max_output=64 * 1024 * 1024):
    """不经 shell，避免控制台闪烁；输出落临时文件以免管道塞满。"""
    check_cancel(stop)
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        process = subprocess.Popen(
            [os.fspath(value) for value in command], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=output, stderr=error,
            creationflags=NO_WINDOW,
        )
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancel(stop)
                if time.monotonic() - started > timeout:
                    raise RuntimeErrorBase("组件进程超时。")
                if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > max_output:
                    raise RuntimeErrorBase("组件输出过多，已停止。")
                time.sleep(0.05)
            check_cancel(stop)
            if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > max_output:
                raise RuntimeErrorBase("组件输出过多，已停止。")
            output.seek(0)
            error.seek(0)
            return process.returncode, (output.read() + b"\n" + error.read()).decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def download_verified(urls, path, expected, stop=None, progress=None, opener=None):
    """HTTPS 流式下载，限制体积/耗时，完整校验成功后才保留文件。"""
    request_open = opener or urllib.request.urlopen  # 自动采用系统/环境代理。
    errors = []
    for url in urls:
        check_cancel(stop)
        if not url.startswith("https://"):
            raise RuntimeErrorBase("组件来源必须使用 HTTPS。")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "FileToolbox-Runtime/1"})
            with request_open(request, timeout=10) as response, open(path, "wb") as target:
                if not response.geturl().startswith("https://"):
                    raise RuntimeErrorBase("组件下载被重定向到非 HTTPS 地址。")
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                digest = hashlib.sha256()
                started = last = time.monotonic()
                while True:
                    check_cancel(stop)
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    done += len(block)
                    if done > 32 * 1024 * 1024 or time.monotonic() - started > 120:
                        raise RuntimeErrorBase("组件下载超出体积或时间限制。")
                    target.write(block)
                    digest.update(block)
                    if progress and time.monotonic() - last > 0.15:
                        progress(f"正在下载 {Path(path).name}：{done / 1024:.0f} KB" + (f" / {total / 1024:.0f} KB" if total else ""))
                        last = time.monotonic()
                if total and done != total:
                    raise RuntimeErrorBase("组件下载不完整。")
                if digest.hexdigest() != expected:
                    raise RuntimeErrorBase("组件 SHA-256 校验失败，未启用。")
            check_cancel(stop)
            return
        except RuntimeCancelled:
            Path(path).unlink(missing_ok=True)
            raise
        except Exception as exc:
            Path(path).unlink(missing_ok=True)
            errors.append(f"{urlsplit(url).hostname}：{exc}")
    raise RuntimeErrorBase("下载失败，请检查网络或系统代理后重试。\n" + "\n".join(errors))


def tool_directory(name, root=None):
    arch = architecture()
    if name == "zstd":
        arch = "x64" if arch == "x64" else "x86"  # ARM64 Windows 可运行 x86 控制台程序。
    version = SEVEN_VERSION if name == "7zip" else ZSTD_VERSION
    return Path(root or runtime_root()) / f"{name}-{version}-{arch}"


def probe(name, executable, stop=None):
    try:
        code, output = run_process([executable, "i" if name == "7zip" else "--version"], stop, timeout=12)
        if code != 0:
            return False
        if name == "7zip":
            return f"7-Zip {SEVEN_VERSION}" in output and all(value in output for value in ("7z", "Rar", "zip"))
        return "Zstandard" in output and ZSTD_VERSION in output
    except RuntimeCancelled:
        raise
    except (OSError, RuntimeErrorBase):
        return False


def installed_tool(name, root=None, stop=None):
    directory = tool_directory(name, root)
    executable = directory / ("7z.exe" if name == "7zip" else "zstd.exe")
    required = ["7z.exe", "7z.dll"] if name == "7zip" else ["zstd.exe"]
    try:
        manifest = json.loads((directory / "verified.json").read_text(encoding="utf-8"))
        if any(digest_file(directory / item) != manifest.get(item) for item in required):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return executable if probe(name, executable, stop) else None


def existing_system_tool(name, stop=None):
    """复用满足版本要求的已有引擎，不写注册表、不改 PATH。"""
    filename = "7z.exe" if name == "7zip" else "zstd.exe"
    candidates = [shutil.which(filename)]
    if name == "7zip":
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                candidates.append(str(Path(base) / "7-Zip" / filename))
    for candidate in dict.fromkeys(value for value in candidates if value):
        if Path(candidate).is_file() and probe(name, candidate, stop):
            return Path(candidate)
    return None


def supplied_by_existing_libraries(name):
    """原有可选库已能覆盖对应功能时，无需另行下载。"""
    archive = sys.modules.get("swift_extract")
    if archive is None:
        return False
    if name == "zstd":
        return bool(archive.zstandard)
    return bool(archive.py7zr and archive.pyzipper and (archive.WINRAR or archive.UNRAR))


def get_tool(name, discover=True):
    """启动检查登记的引擎；CLI 按需检测已配置的组件，绝不暗中联网。"""
    if name in _tools:
        return _tools[name]
    if not discover:
        return None
    if os.name != "nt":
        return None
    try:
        value = installed_tool(name) or existing_system_tool(name)
    except (OSError, RuntimeErrorBase):
        value = None
    if value:
        _tools[name] = value
    return value


def _extract_zstd(package, stage):
    wanted = {"zstd.exe", "LICENSE", "COPYING"}
    with zipfile.ZipFile(package) as archive:
        seen = set()
        for info in archive.infolist():
            basename = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if basename not in wanted:
                continue
            if basename in seen or info.file_size > 16 * 1024 * 1024:
                raise RuntimeErrorBase("Zstd 发布包内容异常。")
            seen.add(basename)
            with archive.open(info) as source, open(stage / basename, "wb") as target:
                shutil.copyfileobj(source, target)
        if "zstd.exe" not in seen:
            raise RuntimeErrorBase("发布包中没有 Zstd 程序。")


def install_tool(name, root=None, stop=None, progress=None, downloader=None):
    if os.name != "nt":
        raise RuntimeErrorBase("便携组件自动配置仅支持 Windows。")
    root = Path(root or runtime_root())
    root.mkdir(parents=True, exist_ok=True)
    # 操作系统锁：退出/崩溃会释放，多开时后启动的窗口等待现有下载。
    import msvcrt
    with open(root / "install.lock", "a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        while True:
            check_cancel(stop)
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if progress:
                    progress("正在等待另一个工具箱窗口完成组件配置…")
                if stop is not None:
                    stop.wait(0.3)
                else:
                    time.sleep(0.3)
        try:
            existing = installed_tool(name, root, stop)
            if existing:
                return existing
            return _install_locked(name, root, stop, progress, downloader or download_verified)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _install_locked(name, root, stop, progress, downloader):
    destination = tool_directory(name, root)
    with tempfile.TemporaryDirectory(prefix="配置-", dir=root) as temp:
        temp = Path(temp)
        stage = temp / "ready"
        stage.mkdir()
        if name == "7zip":
            for which in ("bootstrap", architecture()):
                filename, digest = SEVEN_ASSETS[which]
                urls = [f"https://github.com/ip7z/7zip/releases/download/{SEVEN_VERSION}/{filename}",
                        f"https://www.7-zip.org/a/{filename}"]
                downloader(urls, temp / filename, digest, stop, progress)
            check_cancel(stop)
            if progress:
                progress("7-Zip 下载校验完成，正在解包便携引擎…")
            code, _ = run_process([temp / "7zr.exe", "x", temp / SEVEN_ASSETS[architecture()][0],
                                  f"-o{stage}", "-y", "7z.exe", "7z.dll", "License.txt", "readme.txt"], stop)
            if code:
                raise RuntimeErrorBase("7-Zip 组件解包失败。")
            files = ["7z.exe", "7z.dll"]
        elif name == "zstd":
            arch = "x64" if architecture() == "x64" else "x86"
            filename, digest = ZSTD_ASSETS[arch]
            package = temp / filename
            downloader([f"https://github.com/facebook/zstd/releases/download/v{ZSTD_VERSION}/{filename}"], package, digest, stop, progress)
            _extract_zstd(package, stage)
            shutil.copyfile(Path(__file__).resolve().parent / "assets" / "zstd-license.txt", stage / "LICENSE.txt")
            files = ["zstd.exe"]
        else:
            raise RuntimeErrorBase("未知组件。")
        check_cancel(stop)
        executable = stage / files[0]
        if not probe(name, executable, stop):
            raise RuntimeErrorBase("组件运行验证失败，未启用；可在运行环境窗口重试。")
        manifest = {item: digest_file(stage / item) for item in files}
        (stage / "verified.json").write_text(json.dumps(manifest), encoding="utf-8")
        check_cancel(stop)
        # 旧损坏目录保留到新组件验证成功，发布失败时恢复它。
        previous = temp / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            stage.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    return destination / files[0]


def configure(stop=None, progress=None, root=None, auto_install=True):
    """每次启动只做本地检测；只有缺少/损坏时才下载，失败可逐项重试。"""
    results = []
    for name, title in (("7zip", "7-Zip · 7z / RAR / 加密 ZIP"), ("zstd", "Zstd · tar.zst 压缩")):
        check_cancel(stop)
        try:
            if os.name != "nt":
                results.append((title, "未配置", "自动配置仅支持 Windows；基础功能可用。"))
                continue
            if progress:
                progress(f"正在检测 {title}…")
            tool = installed_tool(name, root, stop)
            if not tool:
                tool = existing_system_tool(name, stop)
            if not tool and supplied_by_existing_libraries(name):
                _tools.pop(name, None)
                results.append((title, "可用", "现有扩展库 / 引擎已覆盖，无需下载。"))
                continue
            if not tool and auto_install:
                if progress:
                    progress(f"缺少 {title}，正在自动下载…")
                tool = install_tool(name, root, stop, progress)
            if tool:
                _tools[name] = tool
                results.append((title, "可用", str(tool)))
            else:
                _tools.pop(name, None)
                results.append((title, "缺少", "点击“检测并补齐”自动下载。"))
        except RuntimeCancelled:
            raise
        except Exception as exc:
            _tools.pop(name, None)
            results.append((title, "配置失败", str(exc)))
    return results
