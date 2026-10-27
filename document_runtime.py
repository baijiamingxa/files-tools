#!/usr/bin/env python3
"""发现或配置 LibreOffice 文档引擎；MSI 只读解包，不运行安装脚本。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path, PureWindowsPath

import runtime_manager as runtime
from file_tools import FileToolError, check_stop

OFFICE_VERSION = "26.8.1"
OFFICE_FILE = "LibreOffice_26.8.1_Win_x86-64.msi"
OFFICE_SHA256 = "c6298b10cfa1748bcbedad94c62ce771e240e5021bfd6d7a967cd571b04b5312"


def office_directory():
    return runtime.runtime_root() / ("LibreOffice-" + OFFICE_VERSION)


def find_office():
    candidates = [office_directory() / "program" / "soffice.com"]
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "LibreOffice" / "program" / "soffice.com")
    for candidate in (shutil.which("soffice.com"), shutil.which("soffice")):
        if candidate:
            candidates.append(Path(candidate))
    return next((path for path in candidates if path.is_file()), None)


def probe_office(path, stop=None):
    try:
        code, output = runtime.run_process([path, "--version"], stop, timeout=20)
        return code == 0 and "LibreOffice" in output
    except runtime.RuntimeCancelled:
        raise
    except (OSError, runtime.RuntimeErrorBase):
        return False


def _safe_piece(value):
    name = value.split(":", 1)[0].split("|")[-1]
    if name in ("", "."):
        return ""
    if PureWindowsPath(name).drive or any(part in ("..", "") for part in name.replace("\\", "/").split("/")) or "/" in name or "\\" in name:
        raise FileToolError("Office 发布包包含不安全路径。")
    return name


def directory_map(records):
    entries = {identity: (parent, name) for identity, parent, name in records}
    anchor = "INSTALLLOCATION" if "INSTALLLOCATION" in entries else "INSTALLDIR"
    if anchor not in entries:
        raise FileToolError("无法识别 LibreOffice 安装目录。")
    resolved = {anchor: Path()}
    def resolve(identity, visiting):
        if identity in resolved:
            return resolved[identity]
        if identity in visiting or identity not in entries:
            return None
        parent, name = entries[identity]
        base = resolve(parent, visiting | {identity})
        if base is None:
            return None
        value = base / _safe_piece(name)
        resolved[identity] = value
        return value
    for identity in entries:
        resolve(identity, set())
    return resolved


class MsiReader:
    def __init__(self, path):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.DWORD = ctypes, wintypes.DWORD
        self.api = ctypes.WinDLL("msi")
        self.api.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT)]
        self.api.MsiDatabaseOpenViewW.argtypes = [wintypes.UINT, wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT)]
        self.api.MsiViewExecute.argtypes = [wintypes.UINT, wintypes.UINT]
        self.api.MsiViewFetch.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.UINT)]
        self.api.MsiRecordGetStringW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.api.MsiRecordReadStream.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        self.api.MsiCloseHandle.argtypes = [wintypes.UINT]
        self.handle = wintypes.UINT()
        self._check(self.api.MsiOpenDatabaseW(str(path), None, ctypes.byref(self.handle)))

    def _check(self, code):
        if code:
            raise FileToolError(f"读取 Office 发布包失败（Windows Installer 数据库返回 {code}）。")

    def query(self, sql, fields):
        view = self.ctypes.c_uint()
        self._check(self.api.MsiDatabaseOpenViewW(self.handle, sql, self.ctypes.byref(view)))
        try:
            self._check(self.api.MsiViewExecute(view, 0))
            while True:
                record = self.ctypes.c_uint()
                code = self.api.MsiViewFetch(view, self.ctypes.byref(record))
                if code == 259:
                    return
                self._check(code)
                try:
                    if fields == 0:
                        yield record
                    else:
                        values = []
                        for index in range(1, fields + 1):
                            length = self.DWORD(32768)
                            buffer = self.ctypes.create_unicode_buffer(length.value)
                            self._check(self.api.MsiRecordGetStringW(record, index, buffer, self.ctypes.byref(length)))
                            values.append(buffer.value)
                        yield values
                finally:
                    self.api.MsiCloseHandle(record)
        finally:
            self.api.MsiCloseHandle(view)

    def extract_stream(self, name, output, stop=None):
        sql = "SELECT `Data` FROM `_Streams` WHERE `Name` = '" + name.replace("'", "''") + "'"
        found = False
        for record in self.query(sql, 0):
            found = True
            with open(output, "xb") as target:
                while True:
                    check_stop(stop)
                    buffer = self.ctypes.create_string_buffer(1024 * 1024)
                    length = self.DWORD(len(buffer))
                    self._check(self.api.MsiRecordReadStream(record, 1, buffer, self.ctypes.byref(length)))
                    if not length.value:
                        break
                    target.write(buffer.raw[:length.value])
        if not found:
            raise FileToolError("发布包中没有预期的压缩数据流。")

    def close(self):
        self.api.MsiCloseHandle(self.handle)


def extract_office_msi(package, destination, stop=None, progress=None):
    reader = MsiReader(package)
    try:
        directories = directory_map(reader.query("SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`", 3))
        components = {identity: directory for identity, directory in reader.query("SELECT `Component`, `Directory_` FROM `Component`", 2)}
        files = list(reader.query("SELECT `File`, `Component_`, `FileName` FROM `File`", 3))
        cabinets = list(reader.query("SELECT `Cabinet` FROM `Media`", 1))
        with tempfile.TemporaryDirectory(prefix="office-cab-", dir=Path(destination).parent) as temporary:
            temporary = Path(temporary)
            raw = temporary / "files"
            raw.mkdir()
            for index, (name,) in enumerate(cabinets):
                check_stop(stop)
                if not name.startswith("#"):
                    raise FileToolError("不支持包含外部压缩文件的 Office 安装包。")
                cab = temporary / f"data-{index}.cab"
                reader.extract_stream(name[1:], cab, stop)
                if progress:
                    progress("正在解压文档引擎（不运行 MSI 安装程序）…")
                code, _ = runtime.run_process([str(Path(os.environ["SystemRoot"]) / "System32" / "expand.exe"), "-F:*", cab, raw], stop, timeout=900)
                if code:
                    raise FileToolError("Office 压缩数据解包失败。")
            destination = Path(destination)
            for identity, component, filename in files:
                check_stop(stop)
                directory = directories.get(components.get(component))
                if directory is None:
                    continue
                source = raw / identity
                if source.is_file():
                    target = destination / directory / _safe_piece(filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    finally:
        reader.close()


def ensure_office(stop=None, progress=None):
    existing = find_office()
    if existing and probe_office(existing, stop):
        return existing
    if os.name != "nt" or runtime.architecture() != "x64":
        raise FileToolError("未找到 LibreOffice；此平台请先安装 LibreOffice 后再转换 Word。")
    root = runtime.runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    # 复用压缩引擎的跨进程锁，防止多开同时配置文档引擎。
    import msvcrt
    with open(root / "office.lock", "a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        while True:
            check_stop(stop)
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if progress:
                    progress("正在等待另一个窗口配置文档引擎…")
                if stop is not None:
                    stop.wait(0.3)
                else:
                    time.sleep(0.3)
        try:
            existing = find_office()
            if existing and probe_office(existing, stop):
                return existing
            with tempfile.TemporaryDirectory(prefix="office-setup-", dir=root) as temporary:
                temp = Path(temporary)
                package = temp / OFFICE_FILE
                if progress:
                    progress("缺少 Word 转 PDF 引擎，正在下载官方 LibreOffice 组件（约 350 MB）…")
                runtime.download_verified([
                    f"https://download.documentfoundation.org/libreoffice/stable/{OFFICE_VERSION}/win/x86_64/{OFFICE_FILE}",
                    f"https://downloadarchive.documentfoundation.org/libreoffice/old/{OFFICE_VERSION}/win/x86_64/{OFFICE_FILE}"],
                    package, OFFICE_SHA256, stop, progress, max_bytes=1024 * 1024 * 1024, max_seconds=1800)
                stage = temp / "ready"
                stage.mkdir()
                extract_office_msi(package, stage, stop, progress)
                executable = stage / "program" / "soffice.com"
                if not executable.is_file() or not probe_office(executable, stop):
                    raise FileToolError("LibreOffice 配置后运行验证失败；可安装官方 LibreOffice 再重试。")
                check_stop(stop)
                destination = office_directory()
                previous = temp / "previous"
                if destination.exists():
                    destination.rename(previous)
                try:
                    stage.rename(destination)
                except OSError:
                    if previous.exists():
                        previous.rename(destination)
                    raise
                return destination / "program" / "soffice.com"
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
