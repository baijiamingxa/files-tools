#!/usr/bin/env python3
"""按用户安装的本地安装器；只安装校验清单列出的文件，可失败回退。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

INSTALL_ID = "TxtEncoder.FileToolbox.Install"
MANIFEST = "安装清单.json"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_name(name):
    value = name.replace("\\", "/")
    if (not value or value.startswith("/") or PureWindowsPath(value).drive or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("安装包包含不安全路径。")
    return str(PurePosixPath(value))


def checked_target(root, relative):
    root = Path(root).absolute()
    target = root / relative_name(relative)
    for parent in (target, *target.parents):
        if parent == root.parent:
            break
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("安装目录中存在链接 / 重解析点，请选择普通文件夹。")
    if os.path.commonpath((str(root), str(target.absolute()))) != str(root):
        raise ValueError("安装目标超出所选目录。")
    return target


def create_payload(source, output, version):
    source = Path(source)
    files = sorted(path for path in source.rglob("*") if path.is_file())
    records = {path.relative_to(source).as_posix(): sha256(path) for path in files}
    metadata = {"id": INSTALL_ID, "version": version, "files": records}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("payload.json", json.dumps(metadata, ensure_ascii=False))
        for path in files:
            archive.write(path, path.relative_to(source).as_posix())
    return metadata


def install_payload(payload, destination, progress=None):
    destination = Path(destination).expanduser().absolute()
    if destination == destination.parent or destination == Path.home().absolute():
        raise ValueError("请为工具箱选择单独的安装文件夹。")
    # 只允许空目录，或由本安装器拥有的目录，避免误改其它软件。
    old_manifest = destination / MANIFEST
    old_bytes = old_manifest.read_bytes() if old_manifest.is_file() else None
    if destination.exists() and any(destination.iterdir()):
        if old_bytes is None or json.loads(old_bytes).get("id") != INSTALL_ID:
            raise ValueError("所选目录已有其他文件，请选择空文件夹或已安装的工具箱目录。")
    checked_target(destination, "文件工具箱.exe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".toolbox-install-", dir=destination.parent) as temporary:
        temp = Path(temporary)
        staged, backup = temp / "staged", temp / "backup"
        staged.mkdir()
        with zipfile.ZipFile(payload) as archive:
            if archive.getinfo("payload.json").file_size > 8 * 1024 * 1024:
                raise ValueError("安装清单过大。")
            data = json.loads(archive.read("payload.json"))
            if data.get("id") != INSTALL_ID or not isinstance(data.get("files"), dict):
                raise ValueError("安装包标识或清单无效。")
            names = [relative_name(name) for name in data["files"]]
            if len(names) != len(set(name.casefold() for name in names)):
                raise ValueError("安装包内存在重名文件。")
            if "文件工具箱.exe" not in names:
                raise ValueError("安装包缺少主程序。")
            total = len(names)
            for index, name in enumerate(names, 1):
                target = checked_target(staged, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                entry = archive.getinfo(name)
                if entry.file_size > 1024 * 1024 * 1024 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("安装包文件类型或大小异常。")
                with archive.open(entry) as source, open(target, "xb") as output:
                    shutil.copyfileobj(source, output)
                if sha256(target) != data["files"][name]:
                    raise ValueError(f"安装文件校验失败：{name}")
                if progress:
                    progress(index, total * 2, "解包并校验：" + name)
        destination.mkdir(parents=True, exist_ok=True)
        committed = []
        moved = []
        try:
            for index, name in enumerate(names, 1):
                target = checked_target(destination, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    old = checked_target(backup, name)
                    old.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, old)
                    moved.append((old, target))
                os.replace(staged / name, target)
                committed.append(target)
                if progress:
                    progress(total + index, total * 2, "安装：" + name)
            data["root"] = str(destination)
            data["shortcuts"] = []
            manifest_temp = destination / (MANIFEST + "." + uuid.uuid4().hex + ".tmp")
            manifest_temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(manifest_temp, old_manifest)
        except Exception:
            for target in reversed(committed):
                target.unlink(missing_ok=True)
            for old, target in reversed(moved):
                os.replace(old, target)
            raise
    return data


def register_installation(directory, metadata, shortcuts=True):
    """仅当前用户的安装记录；不更改任何默认文件关联。"""
    if os.name != "nt":
        return
    import subprocess
    import winreg
    directory = Path(directory).absolute()
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TxtEncoderFileToolbox"
    powershell = str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    uninstall = f'"{powershell}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{directory / "uninstall.ps1"}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        for name, value in {"DisplayName": "文件工具箱", "DisplayVersion": metadata["version"], "Publisher": "文件工具箱开源项目", "InstallLocation": str(directory), "DisplayIcon": str(directory / "文件工具箱.exe"), "UninstallString": uninstall}.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)
    if shortcuts:
        # 参数通过环境变量传递，路径不拼进 PowerShell 代码。
        menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "文件工具箱"
        menu.mkdir(parents=True, exist_ok=True)
        link = menu / "文件工具箱.lnk"
        env = os.environ.copy()
        env.update(TOOLBOX_LINK=str(link), TOOLBOX_EXE=str(directory / "文件工具箱.exe"), TOOLBOX_DIR=str(directory))
        code = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TOOLBOX_LINK);$s.TargetPath=$env:TOOLBOX_EXE;$s.WorkingDirectory=$env:TOOLBOX_DIR;$s.IconLocation=$env:TOOLBOX_EXE+',0';$s.Save()"
        subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", code], env=env, check=True, creationflags=0x08000000)
        metadata["shortcuts"] = [str(link)]
        (directory / MANIFEST).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
