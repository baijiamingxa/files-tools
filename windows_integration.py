#!/usr/bin/env python3
"""在当前用户下安装或移除文件工具箱的 Windows 右键菜单与打开方式。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROG_ID = "TxtEncoder.FileToolbox"
VERB = "TxtEncoder.FileToolbox.Open"
MENU_LABEL = "通过文件工具箱打开"
CLASSES_ROOT = r"Software\Classes"
ARCHIVE_EXTENSIONS = (
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".tgz", ".tbz", ".tbz2", ".txz", ".tzst", ".cab", ".iso",
    ".arj", ".lzh", ".lha", ".jar", ".whl", ".apk", ".001", ".r00", ".z01",
)
EXTENSIONS = (".txt", ".doc", ".docx", ".pdf") + ARCHIVE_EXTENSIONS


def registration_entries(app_dir=None, python_exe=None):
    """生成确定的注册项；不改扩展名默认值或 UserChoice。"""
    if app_dir is None and python_exe is None and getattr(sys, "frozen", False):
        # 打包成 exe 后没有 file_toolbox.py 和解释器，注册项直接指向 exe 本身。
        exe = Path(sys.executable).resolve()
        app_dir = exe.parent
        command = f'"{exe}" "%1"'
        icon = f'"{exe}",0'
    else:
        app_dir = Path(app_dir or Path(__file__).resolve().parent).resolve()
        script = app_dir / "file_toolbox.py"
        interpreter = Path(python_exe or sys.executable).resolve()
        if python_exe is None:
            windowed = interpreter.with_name("pythonw.exe")
            if windowed.is_file():
                interpreter = windowed
        if not script.is_file():
            raise FileNotFoundError(f"找不到工具箱程序：{script}")
        if not interpreter.is_file():
            raise FileNotFoundError(f"找不到 Python：{interpreter}")
        # 直接执行解释器，不经过 cmd；文件名中的空格、&、!、% 都保持原样。
        command = f'"{interpreter}" "{script}" "%1"'
        icon = f'"{interpreter}",0'
        app_icon = app_dir / "assets" / "app-icon.ico"
        if app_icon.is_file():
            icon = f'"{app_icon}",0'
    entries = {
        PROG_ID: {"": "文件工具箱", "FriendlyTypeName": "文件工具箱", "InstallDir": str(app_dir)},
        PROG_ID + r"\DefaultIcon": {"": icon},
        PROG_ID + r"\Application": {"ApplicationName": "文件工具箱", "ApplicationDescription": "下载、压缩解压和文本编码转换"},
        PROG_ID + r"\shell\open": {"": MENU_LABEL, "FriendlyAppName": "文件工具箱"},
        PROG_ID + r"\shell\open\command": {"": command},
    }
    for ext in EXTENSIONS:
        verb = rf"SystemFileAssociations\{ext}\shell\{VERB}"
        entries[verb] = {"": MENU_LABEL, "MUIVerb": MENU_LABEL, "Icon": icon, "MultiSelectModel": "Single"}
        entries[verb + r"\command"] = {"": command}
        entries[ext + r"\OpenWithProgids"] = {PROG_ID: ""}
    return entries


def _registry():
    if os.name != "nt":
        raise OSError("文件右键菜单仅适用于 Windows。")
    import winreg
    return winreg


def _notify_shell():
    import ctypes
    # SHCNE_ASSOCCHANGED / SHCNF_IDLIST，让资源管理器刷新关联缓存。
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def install(app_dir=None, python_exe=None, root_path=CLASSES_ROOT):
    winreg = _registry()
    entries = registration_entries(app_dir, python_exe)
    for path, values in entries.items():
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, root_path + "\\" + path, 0, winreg.KEY_WRITE) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    if root_path == CLASSES_ROOT:
        _notify_shell()
    return len(EXTENSIONS)


def _delete_tree(winreg, path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while winreg.QueryInfoKey(key)[0]:
                _delete_tree(winreg, path + "\\" + winreg.EnumKey(key, 0))
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def uninstall(root_path=CLASSES_ROOT):
    winreg = _registry()
    for ext in EXTENSIONS:
        _delete_tree(winreg, root_path + rf"\SystemFileAssociations\{ext}\shell\{VERB}")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path + rf"\{ext}\OpenWithProgids", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, PROG_ID)
        except FileNotFoundError:
            pass
    _delete_tree(winreg, root_path + "\\" + PROG_ID)
    if root_path == CLASSES_ROOT:
        _notify_shell()


def is_installed(app_dir=None, python_exe=None, root_path=CLASSES_ROOT):
    winreg = _registry()
    for path, values in registration_entries(app_dir, python_exe).items():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path + "\\" + path) as key:
                for name, expected in values.items():
                    actual, _kind = winreg.QueryValueEx(key, name)
                    if actual != expected:
                        return False
        except FileNotFoundError:
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true", help="安装或更新右键菜单与打开方式")
    action.add_argument("--uninstall", action="store_true", help="移除本工具的菜单和打开方式")
    action.add_argument("--status", action="store_true", help="检查是否已为当前目录安装")
    args = parser.parse_args(argv)
    try:
        if args.install:
            count = install()
            print(f"已为当前用户安装右键菜单和打开方式，共 {count} 种文件类型。")
            print(f"右键 TXT 或压缩包 → {MENU_LABEL}。Windows 11 可在“显示更多选项”中查找。")
        elif args.uninstall:
            uninstall()
            print("已移除文件工具箱的右键菜单和打开方式。")
        else:
            installed = is_installed()
            print("已安装（指向当前工具箱目录）" if installed else "尚未安装或需要更新路径")
            return 0 if installed else 1
    except OSError as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    from txt_encoder_core import make_console_tolerant
    make_console_tolerant()
    raise SystemExit(main())
