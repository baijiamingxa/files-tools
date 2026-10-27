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
from dependency_check import DependencyResult, inspect_dependencies, python_result


def check_builtin():
    sample = "文件工具箱运行环境自检".encode("gbk")
    if sample.decode("gbk").encode("gbk") != sample:
        raise RuntimeError("内置编码组件自检失败。")
    for compress, decompress in ((gzip.compress, gzip.decompress), (bz2.compress, bz2.decompress), (lzma.compress, lzma.decompress)):
        if decompress(compress(sample)) != sample:
            raise RuntimeError("内置压缩组件自检失败。")
    if len(hashlib.sha256(sample).digest()) != 32:
        raise RuntimeError("内置哈希组件自检失败。")
    if len(hashlib.new("md5", sample, usedforsecurity=False).digest()) != 16:
        raise RuntimeError("内置 MD5 组件自检失败。")
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
    return ("基础运行环境", "可用", "文本编码、MD5 / SHA-256、ZIP / TAR 的压缩基础组件、HTTPS 已就绪；EXE 自带 Python / Tk。")


class RuntimeController:
    def __init__(self, app, parent, on_ready=None):
        self.app = app
        self.on_ready = on_ready
        self.worker = None
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.results = []
        self.dependencies = []
        self.window = None
        self._timer = None
        self._shutdown = False
        self._active = False
        self._installing = False
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
        self._installing = auto_install
        self.message.set("运行环境：正在检测，缺失的扩展组件将自动下载…" if auto_install else "自检：正在检查本地依赖包与运行组件…")
        self.results = []
        self.dependencies = []
        try:
            gui = DependencyResult("Tkinter / Tcl-Tk", "图形界面", "可用", str(self.app.tk.call("info", "patchlevel")),
                detail="主窗口已成功创建，Tcl / Tk 解释器可调用。")
        except tk.TclError as exc:
            gui = DependencyResult("Tkinter / Tcl-Tk", "图形界面", "自检失败", detail=str(exc))
        self.cancel_btn.configure(state="normal")
        if self.window and self.window.winfo_exists():
            self.retry_btn.configure(state="disabled")
            self.check_btn.configure(state="disabled")
            self.progress.configure(mode="indeterminate", value=0)
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
                dependencies = [python_result(), gui, *inspect_dependencies(self.stop, lambda value: self.events.put(("progress", value)))]
                self.events.put(("results", (results, dependencies)))
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
                self.results, self.dependencies = value
                failed = [item for item in self.results if item[1] != "可用"]
                broken = [item for item in self.dependencies if item.status in ("加载失败", "自检失败")]
                missing = sum(item.status == "未安装（可选）" for item in self.dependencies)
                if failed or broken:
                    self.message.set(f"自检完成：{len(failed)} 个运行组件未就绪，{len(broken)} 个依赖加载或自检失败；请查看详情。")
                else:
                    self.message.set("自检完成：必需组件全部就绪。" + (f" {missing} 个可选包未安装，可使用替代引擎或相应按钮。" if missing else " 所有已安装依赖均可用。"))
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
                    self.check_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100, value=100 if self.results or self.dependencies else 0)
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
        for row in self.dependencies:
            self.detail.insert("end", f"{row.name}：{row.status}\n{row.report()}\n\n")
        self.detail.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self._diagnostic_details = {}
        rows = []
        for name, state, description in self.results:
            version = runtime.SEVEN_VERSION if name.startswith("7-Zip") else runtime.ZSTD_VERSION if name.startswith("Zstd") else ""
            location = description if state == "可用" and description.endswith(".exe") else ""
            if description == "现有扩展库 / 引擎已覆盖，无需下载。":
                state, version, location = "可用（替代）", "", "已有扩展库 / 外部软件"
            rows.append(DependencyResult(name, "内置基础环境" if name == "基础运行环境" else "压缩扩展引擎", state, version, location, description))
        rows.extend(self.dependencies)
        for index, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(index), values=(row.name, row.status, row.version, row.purpose, row.location))
            self._diagnostic_details[str(index)] = row.report()

    def self_check(self):
        self.show()
        if not self.busy():
            self.start(auto_install=False)

    def _select_result(self, event=None):
        selected = self.tree.selection()
        if selected:
            item = selected[0]
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("end", self._diagnostic_details.get(item, ""))
            self.detail.configure(state="disabled")

    def copy_report(self):
        parts = [self.message.get()]
        parts.extend(f"{name}：{state}\n{description}" for name, state, description in self.results)
        parts.extend(f"{row.name}：{row.status}\n{row.report()}" for row in self.dependencies)
        self.app.clipboard_clear()
        self.app.clipboard_append("\n\n".join(parts))

    def show(self):
        if self.window and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            return
        self.window = win = tk.Toplevel(self.app)
        win.title("依赖自检与运行环境")
        win.geometry("1000x660")
        win.minsize(760, 560)
        win.transient(self.app)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="依赖自检", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        description = ttk.Label(frame, text=("“重新自检”只检查本地依赖，不下载。基础环境由 EXE 自带，可选包可以由便携引擎替代。\n"
                               "缺失的压缩引擎可点“安装 / 修复扩展组件”补齐；启动时仍会自动补齐缺失引擎。"), wraplength=930)
        description.pack(anchor="w", fill="x", pady=(8, 12))
        status_label = ttk.Label(frame, textvariable=self.message, wraplength=930)
        status_label.pack(anchor="w", fill="x")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=8)
        if self.busy():
            self.progress.start(12)
        else:
            self.progress.configure(mode="determinate", maximum=100, value=100 if self.results or self.dependencies else 0)
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=("name", "state", "version", "purpose", "location"), show="headings", height=9)
        for name, label, width in (("name", "依赖 / 组件", 170), ("state", "安装 / 运行状态", 140), ("version", "版本", 80), ("purpose", "对应功能", 200), ("location", "安装 / 加载位置", 330)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=70, stretch=name in ("purpose", "location"))
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._select_result)
        selection_hint = ttk.Label(frame, text="选择一行查看完整说明；可选包未安装不等于工具箱不能运行。")
        selection_hint.pack(anchor="w", pady=(8, 2))
        self.detail = tk.Text(frame, wrap="word", height=6, font=("Microsoft YaHei UI", 10))
        self.detail.pack(fill="x")
        self._render()
        download_hint = ttk.Label(frame, text="扩展引擎下载来源：7-Zip / Zstd 官方发布；启用前校验 SHA-256。", wraplength=930)
        download_hint.pack(anchor="w", pady=8)
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.check_btn = ttk.Button(actions, text="重新自检", command=lambda: self.start(auto_install=False), state="disabled" if self.busy() else "normal")
        self.check_btn.pack(side="left")
        self.retry_btn = ttk.Button(actions, text="安装 / 修复扩展组件", command=self.start, state="disabled" if self.busy() else "normal")
        self.retry_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="停止", command=self.cancel).pack(side="left", padx=8)
        ttk.Button(actions, text="复制自检报告", command=self.copy_report).pack(side="left", padx=8)
        ttk.Button(actions, text="关闭", command=win.destroy).pack(side="right")
        # 固定操作区先保留空间，缩小窗口时仅缩短表格。
        actions.pack_configure(side="bottom", before=table_frame)
        download_hint.pack_configure(side="bottom", before=table_frame)
        self.detail.pack_configure(side="bottom", before=table_frame)
        selection_hint.pack_configure(side="bottom", before=table_frame)
        def resize(event):
            if event.widget is win:
                for label in (description, status_label, download_hint):
                    label.configure(wraplength=max(360, event.width - 40))
        win.bind("<Configure>", resize)

    def cancel(self):
        self.stop.set()
        if self.busy():
            self.message.set("运行环境：正在取消，等待当前网络请求 / 检查结束…" if self._installing else "自检：正在取消，等待当前检查结束…")

    def shutdown(self):
        self._shutdown = True
        self.stop.set()
