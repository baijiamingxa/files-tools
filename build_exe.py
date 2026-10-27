#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把文件工具箱打包成程序目录和按用户安装的安装包，无需系统 Python。

用法：
    py -m venv .venv-document-build
    .venv-document-build\Scripts\python.exe -m pip install -r requirements-build.txt
    .venv-document-build\Scripts\python.exe build_exe.py
产物：
    dist/文件工具箱安装包.exe，以及 dist/ 下的完整程序目录
"""

from __future__ import annotations

import subprocess
import sys
import argparse
import importlib.metadata
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "文件工具箱"
ASSETS = HERE / "assets"


def main():
    parser = argparse.ArgumentParser(description="生成程序目录和按用户安装的安装包")
    parser.add_argument("--app-only", action="store_true", help="只生成程序目录")
    options = parser.parse_args()
    try:
        import PyInstaller  # noqa: F401
        import pymupdf
        import pdf2docx  # noqa: F401
    except ImportError:
        print("缺少文档打包依赖。请在独立环境安装 requirements-documents.txt 和 PyInstaller 后重试。")
        return 1
    if pymupdf.VersionBind != "1.26.7":
        print("请使用 requirements-documents.txt 固定的 PyMuPDF 版本构建，避免引入无关的布局模型组件。")
        return 1
    from desktop_support import APP_VERSION
    from installer_core import create_payload
    icon = ASSETS / "app-icon.ico"
    resources = [icon, ASSETS / "app-icon-32.png", ASSETS / "app-icon-64.png", ASSETS / "zstd-license.txt"]
    for path in resources:
        if not path.is_file():
            print(f"找不到打包资源：{path}，请恢复该资源后重试（图标可用 build_icon.ps1 重建）。")
            return 1
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(icon),
        "--distpath", str(HERE / "build" / "document-app"),
    ]
    for path in resources:
        args.extend(["--add-data", f"{path}:assets"])
    for package in ("pdf2docx", "PyMuPDF", "python-docx", "numpy", "opencv-python-headless", "py7zr", "pyzipper", "zstandard", "windnd"):
        args.extend(["--copy-metadata", package])
    # 字体配置、许可证和 OpenCV 的运行数据交给官方 PyInstaller hook 收集。
    license_dir = HERE / "build" / "third-party-licenses"
    license_dir.mkdir(parents=True, exist_ok=True)
    for package in ("pdf2docx", "PyMuPDF", "python-docx", "numpy", "opencv-python-headless", "lxml", "fonttools", "py7zr", "pyzipper", "zstandard", "windnd"):
        distribution = importlib.metadata.distribution(package)
        target_dir = license_dir / package
        target_dir.mkdir(exist_ok=True)
        (target_dir / "来源.txt").write_text(f"{package} {distribution.version}\nhttps://pypi.org/project/{package}/{distribution.version}/\n", encoding="utf-8")
        for item in distribution.files or []:
            if any(word in str(item).lower() for word in ("license", "copying")):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    target = target_dir / source.name
                    if not target.exists():
                        shutil.copyfile(source, target)
    args.extend(["--add-data", f"{license_dir}:licenses"])
    args.append("file_toolbox.py")
    print("开始打包，通常需要一两分钟…")
    result = subprocess.run(args, cwd=HERE)
    if result.returncode != 0:
        print("打包失败，请把上面的输出发给我。")
        return result.returncode
    staged = HERE / "build" / "document-app" / APP_NAME
    shutil.copyfile(HERE / "uninstall.ps1", staged / "uninstall.ps1")
    shutil.copyfile(HERE / "LICENSE", staged / "LICENSE")
    # 保留原双击路径，旁边的 _internal 是必需的运行库目录。
    destination = HERE / "dist"
    destination.mkdir(exist_ok=True)
    shutil.copytree(staged, destination, dirs_exist_ok=True)
    exe = destination / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"打包命令结束，但没有在 {exe} 找到 exe，请检查 dist 目录。")
        return 1
    total = sum(path.stat().st_size for path in staged.rglob("*") if path.is_file())
    print(f"程序目录已生成：{exe}（整个目录 {total / 1024 / 1024:.1f} MB，不能单独移动 EXE）")
    if not options.app_only:
        package_dir = HERE / "build" / "installer-payload"
        package_dir.mkdir(parents=True, exist_ok=True)
        archive = package_dir / "application.zip"
        create_payload(staged, archive, APP_VERSION)
        installer_args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "文件工具箱安装包", "--icon", str(icon),
            "--add-data", f"{archive}:payload", "--add-data", f"{ASSETS / 'app-icon-64.png'}:assets", "installer_app.py"]
        result = subprocess.run(installer_args, cwd=HERE)
        if result.returncode:
            return result.returncode
        installer = HERE / "dist" / "文件工具箱安装包.exe"
        print(f"安装包已生成：{installer}（{installer.stat().st_size / 1024 / 1024:.1f} MB）")
    print("首次运行 Windows 可能弹出 SmartScreen 提示，点“仍要运行”即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
