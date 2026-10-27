#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把文件工具箱打包成单文件 exe，双击即可运行，无需安装 Python。

用法：
    py -m pip install pyinstaller    （只需装一次）
    py build_exe.py
产物：
    dist/文件工具箱.exe
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "文件工具箱"
ASSETS = HERE / "assets"


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未找到 PyInstaller。请先执行：py -m pip install pyinstaller")
        return 1
    icon = ASSETS / "app-icon.ico"
    resources = [icon, ASSETS / "app-icon-32.png", ASSETS / "app-icon-64.png", ASSETS / "zstd-license.txt"]
    for path in resources:
        if not path.is_file():
            print(f"找不到打包资源：{path}，请恢复该资源后重试（图标可用 build_icon.ps1 重建）。")
            return 1
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", APP_NAME,
        "--icon", str(icon),
    ]
    for path in resources:
        args.extend(["--add-data", f"{path}:assets"])
    args.append("file_toolbox.py")
    print("开始打包，通常需要一两分钟…")
    result = subprocess.run(args, cwd=HERE)
    if result.returncode != 0:
        print("打包失败，请把上面的输出发给我。")
        return result.returncode
    exe = HERE / "dist" / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"打包命令结束，但没有在 {exe} 找到 exe，请检查 dist 目录。")
        return 1
    print(f"打包完成：{exe}")
    print("首次运行 Windows 可能弹出 SmartScreen 提示，点“仍要运行”即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
