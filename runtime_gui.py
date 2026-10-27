#!/usr/bin/env python3
"""首次启动组件检测与可取消的自动配置界面。"""
from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import lzma
import queue
import ssl
import threading
import tarfile
import tkinter as tk
import zipfile
from tkinter import ttk

import runtime_manager as runtime


def check_builtin():
    sample = "文件工具箱运行环境自检".encode("gbk")
    if sample.decode("gbk").encode("gbk") != sample:
        raise RuntimeError("内置编码组件自检失败。")
    for compress, decompress in ((gzip.compress, gzip.decompress), (bz2.compress, bz2.decompress), (lzma.compress, lzma.decompress)):
        if decompress(compress(sample)) != sample:
            raise RuntimeError("内置压缩组件自检失败。")
    if len(hashlib.sha256(sample).digest()) != 32:
        raise RuntimeError("内置哈希组件自检失败。")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("check.txt", sample)
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        if archive.read("check.txt") != sample:
            raise RuntimeError("内置 ZIP 组件自检失败。")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("check.txt")
        info.size = len(sample)
        archive.addfile(info, io.BytesIO(sample))
    with tarfile.open(fileobj=io.BytesIO(buffer.getvalue())) as archive:
        if archive.extractfile("check.txt").read() != sample:
            raise RuntimeError("内置 TAR 组件自检失败。")
    ssl.create_default_context()
    return ("基础运行环境", "可用", "文本编码、SHA-256、ZIP / TAR 的压缩基础组件、HTTPS 已就绪；EXE 自带 Python / Tk。")


class RuntimeController:
    def __init__(self, app, parent, on_ready=None):
        self.app = app
        self.on_ready = on_ready
        self.worker = None
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.results = []
        self.window = None
        self._timer = None
        self._shutdown = False
        self._active = False
        self.message = tk.StringVar(app, value="运行环境：等待首次检测…")
        self.bar = ttk.Frame(parent, padding=(14, 2, 14, 6))
        self.bar.pack(fill="x")
        ttk.Label(self.bar, textvariable=self.message).pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(self.bar, text="取消配置", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.details_btn = ttk.Button(self.bar, text="运行环境…", command=self.show)
        self.details_btn.pack(side="right", padx=6)

    def busy(self):
        return self._active or bool(self.worker and self.worker.is_alive())

    def start(self, auto_install=True):
        if self.busy() or self._shutdown:
            return
        self.stop.clear()
        self._active = True
        self.message.set("运行环境：正在检测，缺失的扩展组件将自动下载…")
        self.results = []
        self.cancel_btn.configure(state="normal")
        if self.window and self.window.winfo_exists():
            self.retry_btn.configure(state="disabled")
            self.progress.start(12)
            self._render()

        def work():
            results = []
            try:
                try:
                    results.append(check_builtin())
                except Exception as exc:
                    results.append(("基础运行环境", "自检失败", str(exc)))
                results.extend(runtime.configure(self.stop, lambda value: self.events.put(("progress", value)), auto_install=auto_install))
                self.events.put(("results", results))
            except runtime.RuntimeCancelled as exc:
                self.events.put(("cancelled", str(exc)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("done", None))

        self.worker = threading.Thread(target=work, name="运行环境自检", daemon=True)
        self.worker.start()
        self._poll()

    def _poll(self):
        self._timer = None
        completed = False
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                self.message.set("运行环境：" + value.replace("\n", " "))
            elif event == "results":
                self.results = value
                failed = [item for item in value if item[1] != "可用"]
                self.message.set("运行环境：全部就绪。" if not failed else "运行环境：部分组件尚未就绪，可查看详情后重试。")
                self._render()
            elif event in ("cancelled", "error"):
                self.message.set("运行环境：" + value.splitlines()[0])
                self.results.append(("自动配置", "已取消" if event == "cancelled" else "失败", value))
                self._render()
            elif event == "done":
                completed = True
                self._active = False
                self.cancel_btn.configure(state="disabled")
                if self.window and self.window.winfo_exists():
                    self.retry_btn.configure(state="normal")
                    self.progress.stop()
                if self.on_ready and not self._shutdown:
                    self.on_ready()
        if not completed:
            self._timer = self.app.after(80, self._poll)

    def _render(self):
        if not self.window or not self.window.winfo_exists():
            return
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for name, state, description in self.results:
            self.detail.insert("end", f"{name}：{state}\n{description}\n\n")
        self.detail.configure(state="disabled")

    def show(self):
        if self.window and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            return
        self.window = win = tk.Toplevel(self.app)
        win.title("运行环境自检与自动配置")
        win.geometry("760x500")
        win.minsize(640, 420)
        win.transient(self.app)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="运行环境", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text=("每次启动先检查本地组件，缺失或损坏时自动下载。可随时取消，稍后重试。\n"
                               "组件只放在当前用户的工具箱目录，不需要管理员权限。"), wraplength=700).pack(anchor="w", pady=(8, 12))
        ttk.Label(frame, textvariable=self.message, wraplength=700).pack(anchor="w")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=8)
        if self.busy():
            self.progress.start(12)
        self.detail = tk.Text(frame, wrap="word", height=9, font=("Microsoft YaHei UI", 10))
        self.detail.pack(fill="both", expand=True)
        self._render()
        ttk.Label(frame, text="下载来源：7-zip.org 官方 GitHub、facebook/zstd 官方 GitHub；启用前校验 SHA-256。", wraplength=700).pack(anchor="w", pady=8)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.retry_btn = ttk.Button(actions, text="检测并补齐", command=self.start, state="disabled" if self.busy() else "normal")
        self.retry_btn.pack(side="left")
        ttk.Button(actions, text="取消当前配置", command=self.cancel).pack(side="left", padx=8)
        ttk.Button(actions, text="关闭", command=win.destroy).pack(side="right")

    def cancel(self):
        self.stop.set()
        if self.busy():
            self.message.set("运行环境：正在取消，等待当前网络请求结束…")

    def shutdown(self):
        self._shutdown = True
        self.stop.set()
