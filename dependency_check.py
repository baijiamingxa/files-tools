#!/usr/bin/env python3
"""可选依赖的真实导入与小样本运行检查；不调用 pip，不访问网络。"""
from __future__ import annotations

import importlib
from importlib import metadata
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import runtime_manager as runtime


@dataclass
class DependencyResult:
    name: str
    purpose: str
    status: str
    version: str = ""
    location: str = ""
    detail: str = ""

    def report(self):
        fields = [f"用途：{self.purpose}"]
        if self.version:
            fields.append(f"版本：{self.version}")
        if self.location:
            fields.append(f"位置：{self.location}")
        if self.detail:
            fields.append(self.detail)
        return "\n".join(fields)


PACKAGES = (
    ("py7zr", "7z 压缩、解压与加密", "可由便携 7-Zip 引擎提供对应功能。"),
    ("pyzipper", "AES ZIP 加密压缩与解压", "可由便携 7-Zip 引擎提供对应功能。"),
    ("zstandard", "Zstd / tar.zst 压缩与解压", "可由便携 Zstd 和 7-Zip 引擎提供对应功能。"),
    ("windnd", "Windows 窗口拖放文件", "未安装时仍可使用“添加压缩包 / 选择文件”按钮。"),
)


def _probe(name, module):
    sample = "文件工具箱依赖自检".encode("utf-8")
    buffer = io.BytesIO()
    if name == "py7zr":
        with module.SevenZipFile(buffer, "w") as archive:
            archive.writestr(sample, "check.txt")
        with module.SevenZipFile(io.BytesIO(buffer.getvalue()), "r") as archive:
            if archive.getnames() != ["check.txt"] or archive.testzip() is not None:
                raise ValueError("7z 写入 / 解压校验未通过。")
    elif name == "pyzipper":
        password = b"FileToolbox-test"
        with module.AESZipFile(buffer, "w", compression=module.ZIP_DEFLATED, encryption=module.WZ_AES) as archive:
            archive.setpassword(password)
            archive.writestr("check.txt", sample)
        with module.AESZipFile(io.BytesIO(buffer.getvalue())) as archive:
            archive.setpassword(password)
            if archive.read("check.txt") != sample:
                raise ValueError("AES ZIP 加密 / 解密内容不一致。")
    elif name == "zstandard":
        data = module.ZstdCompressor().compress(sample)
        if module.ZstdDecompressor().decompress(data) != sample:
            raise ValueError("Zstd 压缩 / 解压内容不一致。")
    elif name == "windnd" and not callable(getattr(module, "hook_dropfiles", None)):
        raise ValueError("windnd 缺少 hook_dropfiles 接口。")


def inspect_dependencies(stop=None, progress=None, importer=None):
    """每项隔离失败；缺少包与传递依赖/DLL 加载失败分别展示。"""
    importer = importer or importlib.import_module
    rows = []
    for name, purpose, fallback in PACKAGES:
        runtime.check_cancel(stop)
        if progress:
            progress(f"正在检查依赖包 {name}…")
        if name == "windnd" and os.name != "nt":
            rows.append(DependencyResult(name, purpose, "不适用", detail="此扩展仅用于 Windows 拖放。"))
            continue
        try:
            module = importer(name)
        except ModuleNotFoundError as exc:
            missing = exc.name == name
            rows.append(DependencyResult(name, purpose, "未安装（可选）" if missing else "加载失败",
                detail=fallback if missing else f"缺少传递依赖 {exc.name}：{exc}\n{fallback}"))
            continue
        except Exception as exc:
            rows.append(DependencyResult(name, purpose, "加载失败", detail=f"{exc}\n{fallback}"))
            continue
        runtime.check_cancel(stop)
        version = getattr(module, "__version__", None)
        if version is None:
            try:
                version = metadata.version(name)
            except Exception:
                version = "未提供版本号"
        version = str(version)
        location = str(getattr(module, "__file__", "内置模块"))
        try:
            _probe(name, module)
            detail = "已成功导入并通过样本运行检查。" if name != "windnd" else "已成功导入，拖放接口可调用；未注册额外窗口钩子。"
            if getattr(sys, "frozen", False):
                detail += " 此包由 EXE 自带，无需另行 pip 安装。"
            rows.append(DependencyResult(name, purpose, "可用", version, location, detail))
        except Exception as exc:
            rows.append(DependencyResult(name, purpose, "自检失败", version, location,
                f"包可以导入，但样本运行失败：{exc}\n{fallback}"))
    return rows


def python_result():
    return DependencyResult("Python", "工具箱解释器", "可用", sys.version.split()[0], str(Path(sys.executable)),
        "EXE 自带解释器，无需在系统中另装 Python。" if getattr(sys, "frozen", False) else "当前为源码运行，使用上述解释器。")
