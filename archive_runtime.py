#!/usr/bin/env python3
"""工具箱便携 7-Zip / Zstd 的压缩解压适配；不向冻结 EXE 内 pip 安装。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path, PureWindowsPath

import runtime_manager as runtime


class _JobStop:
    def __init__(self, job):
        self.job = job

    def is_set(self):
        return self.job.cancel


def _check(job):
    import swift_extract as se
    if job.cancel:
        raise se.Cancelled()


def _run(command, job, cwd=None):
    import swift_extract as se
    try:
        code, output = runtime.run_process(command, _JobStop(job), timeout=24 * 3600, cwd=cwd)
    except runtime.RuntimeCancelled:
        raise se.Cancelled() from None
    except (runtime.RuntimeErrorBase, OSError) as exc:
        raise se.EngineError(str(exc)) from exc
    if code:
        lowered = output.lower()
        if "wrong password" in lowered or "password is incorrect" in lowered or "encrypted archive" in lowered:
            raise se.NeedPassword("压缩包需要密码，或密码不正确。")
        raise se.EngineError(f"扩展引擎返回码 {code}：{output[-2000:].strip()}")
    return output


def validate_listing(output):
    """外部引擎处理前拒绝绝对路径、上跳、NTFS 数据流和链接。"""
    import swift_extract as se
    for line in output.splitlines():
        if line.startswith("Path = "):
            path = line[7:].replace("\\", "/")
            if (not path or path.startswith("/") or PureWindowsPath(path).drive
                    or ".." in path.split("/") or ":" in path or "\x00" in path):
                raise se.EngineError(f"压缩包含不安全路径：{path}")
        if (line.startswith(("Symbolic Link = ", "Hard Link = ", "Reparse = "))
                or (line.startswith("Attributes = ") and "l" in line.split(" = ", 1)[1])):
            raise se.EngineError("扩展引擎不解压符号链接、硬链接或重解析点。")


def _is_link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_directory(path):
    import swift_extract as se
    path = path.absolute()
    for parent in reversed((path,) + tuple(path.parents)):
        if parent.exists() or parent.is_symlink():
            if _is_link(parent) or not parent.is_dir():
                raise se.EngineError(f"输出目录含链接或非目录项目：{parent}")
        else:
            parent.mkdir()


def _copy(source, target, job):
    import swift_extract as se
    _safe_directory(target.parent)
    if target.is_symlink() or (target.exists() and _is_link(target)):
        raise se.EngineError(f"输出位置是链接：{target}")
    # 临时文件与目标同目录，取消/复制失败不覆盖原有文件。
    descriptor, temp = tempfile.mkstemp(prefix=".toolbox-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as out, open(source, "rb") as incoming:
            while True:
                _check(job)
                data = incoming.read(se.CHUNK)
                if not data:
                    break
                out.write(data)
                job.add_bytes(len(data))
        _check(job)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def _merge(stage, destination, job):
    import swift_extract as se
    _safe_directory(destination)
    entries = sorted(stage.rglob("*"))
    if any(_is_link(path) for path in entries):
        raise se.EngineError("扩展引擎输出包含链接，已停止复制。")
    job.files_total = sum(path.is_file() for path in entries)
    job.bytes_total = sum(path.stat().st_size for path in entries if path.is_file())
    job.bytes_done = job.files_done = 0
    for path in entries:
        _check(job)
        target = destination / path.relative_to(stage)
        if path.is_dir():
            _safe_directory(target)
        elif path.is_file():
            _copy(path, target, job)
            job.add_files()


def extract(job, opts):
    import swift_extract as se
    executable = runtime.get_tool("7zip")
    if not executable:
        raise se.EngineError("缺少扩展引擎，请在工具箱右上角“运行环境”中检测并补齐。")
    job.backend = "7-Zip 便携引擎"
    archive = str(Path(job.path).absolute())
    password = f"-p{job.password}" if job.password else "-p-"
    common = ["-y", "-bd", "-bsp0", "-sccUTF-8", password]
    listing = _run([executable, "l", "-slt", "-ba", *common, "--", archive], job)
    validate_listing(listing)
    with tempfile.TemporaryDirectory(prefix="toolbox-extract-") as temp:
        stage = Path(temp)
        _run([executable, "x", *common, f"-o{stage}", "--", archive], job)
        # zst 解压若得到 tar，继续用已有的标准库 tar 引擎展开。
        files = list(stage.iterdir())
        if job.kind == "zst" and len(files) == 1 and files[0].is_file() and se.detect_format(str(files[0])) == "tar":
            inner_dest = stage / "展开内容"
            original_path, original_kind, original_dest = job.path, job.kind, job.dest
            try:
                job.path, job.kind, job.dest = str(files[0]), "tar", str(inner_dest)
                se.run_stream(job, opts)
            finally:
                job.path, job.kind, job.dest = original_path, original_kind, original_dest
            stage = inner_dest
        _merge(stage, Path(job.dest), job)


def compress(job, opts):
    import swift_extract as se
    executable = runtime.get_tool("7zip")
    if not executable:
        raise se.EngineError("缺少扩展引擎，请在“运行环境”中检测并补齐。")
    job.backend = "7-Zip 便携引擎"
    if job.kind == "zip" and job.password and not job.password.isascii():
        raise se.EngineError("7-Zip 的 ZIP 加密密码只支持 ASCII 字符；中文密码请改选 7z 格式。")
    entries = se._gather_entries(job.sources)
    if not entries:
        raise se.EngineError("没有可压缩的内容。")
    output = Path(job.path).absolute()
    _safe_directory(output.parent)
    with tempfile.TemporaryDirectory(prefix=".toolbox-compress-", dir=output.parent) as temp:
        temp = Path(temp)
        stage = temp / "input"
        stage.mkdir()
        for source, arcname, _, is_dir in entries:
            _check(job)
            target = stage / arcname
            if is_dir:
                _safe_directory(target)
            else:
                _copy(Path(source), target, job)
        archive = temp / "output.part"
        command = [executable, "a", f"-t{job.kind}", "-y", "-bd", "-bsp0", "-sccUTF-8", f"-mx={se.LEVELS[job.level]}"]
        if job.password:
            command += [f"-p{job.password}", "-mhe=on" if job.kind == "7z" else "-mem=AES256"]
        _run([*command, "--", archive, "."], job, cwd=stage)
        _check(job)
        os.replace(archive, output)
    job.files_total = job.files_done = sum(not entry[3] for entry in entries)
    job.bytes_total = job.bytes_done = sum(entry[2] for entry in entries)


def compress_tar_zstd(job, opts):
    import swift_extract as se
    executable = runtime.get_tool("zstd")
    if not executable:
        raise se.EngineError("缺少 Zstd 组件，请在“运行环境”中检测并补齐。")
    job.backend = "Zstd 便携引擎"
    output = Path(job.path).absolute()
    _safe_directory(output.parent)
    with tempfile.TemporaryDirectory(prefix=".toolbox-zstd-", dir=output.parent) as temp:
        temp = Path(temp)
        original_path, original_kind = job.path, job.kind
        try:
            job.path, job.kind = str(temp / "input.tar"), "tar"
            se.run_tar_compress(job, opts)
        finally:
            job.path, job.kind = original_path, original_kind
        level = {"store": 1, "fast": 1, "normal": 3, "max": 19}[job.level]
        _run([executable, f"-{level}", "-q", "-f", temp / "input.tar", "-o", temp / "output.zst"], job)
        _check(job)
        os.replace(temp / "output.zst", output)
