#!/usr/bin/env python3
"""Word / PDF 批量转换：隔离工作进程、保留源文件、拒绝覆盖输出。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from batch_rename import file_signature
from file_tools import FileToolCancelled, FileToolError, check_stop, file_hashes, key, _publish_new
import runtime_manager as runtime

WORKER_FLAG = "--document-worker"
DOCUMENT_EXTENSIONS = (".doc", ".docx", ".pdf")


@dataclass(frozen=True)
class DocumentOptions:
    mode: str = "auto"
    output_dir: str = ""
    password: str = ""
    timeout: int = 1200

    def validate(self):
        if self.mode not in ("auto", "word_pdf", "pdf_word"):
            raise FileToolError("请选择 Word 转 PDF 或 PDF 转 Word。")
        if not 30 <= self.timeout <= 7200:
            raise FileToolError("单文件超时应为 0.5～120 分钟。")


@dataclass
class DocumentRow:
    path: Path
    destination: Path
    mode: str
    signature: tuple = ()
    digest: str = ""
    status: str = "待转换"
    detail: str = ""
    error: str = ""
    options: object = None


def build_document_plan(paths, options, stop=None, progress=None):
    options.validate()
    rows = []
    for index, value in enumerate(paths, 1):
        check_stop(stop)
        path = Path(value).absolute()
        mode = "pdf_word" if path.suffix.lower() == ".pdf" else "word_pdf"
        output = Path(options.output_dir).expanduser().absolute() if options.output_dir else path.parent / "文档转换结果"
        destination = output / (path.stem + (".docx" if mode == "pdf_word" else ".pdf"))
        row = DocumentRow(path, destination, mode, options=options)
        try:
            if path.suffix.lower() not in DOCUMENT_EXTENSIONS:
                raise FileToolError("只支持 DOC、DOCX 和 PDF 文件。")
            if options.mode != "auto" and options.mode != mode:
                row.status, row.detail = "已跳过", "与所选转换方向不符。"
            else:
                row.signature = file_signature(path)
                row.digest = file_hashes(path, ("sha256",), stop)["sha256"]
                if destination.exists() or destination.is_symlink():
                    raise FileToolError("输出已存在，不会覆盖；请更换输出目录或移动旧结果。")
                if path.suffix.lower() == ".docx" and not zipfile.is_zipfile(path):
                    raise FileToolError("DOCX 已加密或文件格式无效，请先解密后再转换。")
        except FileToolCancelled:
            raise
        except Exception as exc:
            row.status, row.error = "无法转换", str(exc)
        rows.append(row)
        if progress:
            progress(index, len(paths), path.name)
    targets = {}
    for row in rows:
        if row.status == "待转换":
            targets.setdefault(key(row.destination), []).append(row)
    for group in targets.values():
        if len(group) > 1:
            for row in group:
                row.status, row.error = "无法转换", "多个源文件将生成同名结果，请分批或分别选择输出目录。"
    return rows


class ProcessGroup:
    """只管理本次转换及其子进程，不按进程名称终止 Office。"""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise FileToolError("无法创建转换进程保护组。")

    def attach(self, process):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise FileToolError("无法保护转换子进程，本次未开始转换。")

    def close(self, process=None):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
        elif process is not None and os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _worker_command(job_path):
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG, str(job_path)]
    return [sys.executable, str(Path(__file__).with_name("document_worker.py")), str(job_path)]


def run_document_worker(job, workdir, stop=None, progress=None, timeout=1200):
    workdir = Path(workdir)
    job_path = workdir / "job.json"
    gate = workdir / "start.gate"
    job.update(gate=str(gate), result=str(workdir / "result.json"), progress=str(workdir / "progress.json"))
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    group, process = ProcessGroup(), None
    try:
        with open(workdir / "worker.log", "wb") as log:
            process = subprocess.Popen(_worker_command(job_path), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=runtime.NO_WINDOW, start_new_session=os.name != "nt")
            group.attach(process)
            gate.touch()  # 子进程在纳入保护组之前不处理文件或启动 LibreOffice。
            started, last_message = time.monotonic(), ""
            while process.poll() is None:
                check_stop(stop)
                if time.monotonic() - started > timeout:
                    raise FileToolError("转换超时，已停止本次转换；源文件保留。")
                try:
                    message = (workdir / "progress.json").read_text(encoding="utf-8")
                    if message != last_message and progress:
                        progress(json.loads(message).get("message", "转换中…"))
                        last_message = message
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            check_stop(stop)
        try:
            result = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FileToolError(f"转换引擎异常退出（{process.returncode}），未发布输出。") from exc
        if process.returncode != 0 or not result.get("ok"):
            raise FileToolError(str(result.get("error", "转换失败。")))
        return result
    finally:
        group.close(process)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()


def execute_document_plan(rows, options, stop=None, progress=None):
    options.validate()
    office = None
    office_error = ""
    if any(row.mode == "word_pdf" and row.status == "待转换" and not row.error for row in rows):
        from document_runtime import ensure_office
        try:
            office = ensure_office(stop=stop, progress=(lambda text: progress(0, len(rows), text)) if progress else None)
        except (FileToolCancelled, runtime.RuntimeCancelled):
            raise FileToolCancelled("已取消文档引擎配置。") from None
        except Exception as exc:
            office_error = str(exc)
    for index, row in enumerate(rows, 1):
        check_stop(stop)
        if row.status != "待转换" or row.error:
            continue
        try:
            if row.mode == "word_pdf" and office_error:
                raise FileToolError(office_error)
            if row.options != options or file_signature(row.path) != row.signature:
                raise FileToolError("文件或规则与预览不一致，请重新扫描。")
            row.destination.parent.mkdir(parents=True, exist_ok=True)
            if row.destination.exists() or row.destination.is_symlink():
                raise FileToolError("输出已被其他文件占用，未覆盖。")
            with tempfile.TemporaryDirectory(prefix=".toolbox-document-", dir=row.destination.parent) as temp:
                work = Path(temp)
                source = work / ("input" + row.path.suffix.lower())
                digest = hashlib.sha256()
                with open(row.path, "rb") as incoming, open(source, "xb") as copy:
                    while True:
                        check_stop(stop)
                        block = incoming.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        copy.write(block)
                if digest.hexdigest() != row.digest or file_signature(row.path) != row.signature:
                    raise FileToolError("文件内容已改变，请重新扫描。")
                output = work / ("output.docx" if row.mode == "pdf_word" else "output.pdf")
                row.status = "转换中"
                result = run_document_worker(dict(mode=row.mode, source=str(source), output=str(output), password=options.password,
                    office=str(office or "")), work, stop,
                    (lambda detail: progress(index - 1, len(rows), row.path.name + " · " + detail)) if progress else None, options.timeout)
                if not output.is_file() or output.stat().st_size == 0:
                    raise FileToolError("引擎没有生成有效结果。")
                if row.mode == "pdf_word":
                    with zipfile.ZipFile(output) as package:
                        if "word/document.xml" not in package.namelist() or package.testzip() is not None:
                            raise FileToolError("生成的 Word 文件校验失败。")
                else:
                    with open(output, "rb") as source_pdf:
                        if source_pdf.read(5) != b"%PDF-":
                            raise FileToolError("生成的 PDF 文件格式无效。")
                check_stop(stop)
                _publish_new(output, row.destination)
                row.status = "已完成"
                row.detail = result.get("detail", "转换完成。")
        except FileToolCancelled:
            row.status = "已取消"
            raise
        except Exception as exc:
            row.status, row.error = "失败", str(exc)
        if progress:
            progress(index, len(rows), row.path.name)
    return rows
