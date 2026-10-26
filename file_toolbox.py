#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件工具箱：在同一窗口下载、压缩解压和转换文本编码。"""

from __future__ import annotations

import sys
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

import direct_downloader
import swift_extract
from txt_encoder_gui import EncoderPanel


class ToolboxApp(tk.Tk):
    def __init__(self, config_path=None):
        super().__init__()
        self.title("文件工具箱 · 下载 / 压缩解压 / 文本转码")
        width = max(1100, min(1200, self.winfo_screenwidth() - 60))
        height = max(660, min(860, self.winfo_screenheight() - 100))
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2 - 30)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(1100, 660)
        self._closing = False
        self.last_download = None

        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="文件工具箱", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ttk.Label(header, text="下载 → 解压 → 转码，也可以单独使用每个工具").pack(side="left", padx=20)

        self.status_var = tk.StringVar(self, value="就绪。结果传到下一页后，由你检查并开始处理。")
        ttk.Label(self, textvariable=self.status_var, padding=(12, 6)).pack(side="bottom", fill="x")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8)
        self.download_tab = ttk.Frame(self.tabs)
        self.archive_tab = ttk.Frame(self.tabs)
        self.encoder = EncoderPanel(self.tabs)
        self.tabs.add(self.download_tab, text="  直链下载  ")
        self.tabs.add(self.archive_tab, text="  压缩 / 解压  ")
        self.tabs.add(self.encoder, text="  文本转码  ")

        actions = ttk.Frame(self.download_tab, padding=8)
        actions.pack(fill="x")
        self.extract_download_btn = ttk.Button(
            actions, text="下载结果 → 解压", state="disabled", command=self.send_download_to_archive)
        self.extract_download_btn.pack(side="left")
        self.encode_download_btn = ttk.Button(
            actions, text="下载结果 → 转码", state="disabled", command=self.send_download_to_encoder)
        self.encode_download_btn.pack(side="left", padx=8)
        self.download = direct_downloader.App(
            self.download_tab, embedded=True, on_complete=self._download_completed)
        self.archive = swift_extract.run_gui(
            parent=self.archive_tab, config_path=config_path, on_output=self.send_to_encoder)

        self.protocol("WM_DELETE_WINDOW", self.request_close)

    def _download_completed(self, path):
        self.last_download = Path(path)
        self.extract_download_btn.configure(state="normal")
        self.encode_download_btn.configure(state="normal")
        self.status_var.set(f"下载完成：{path}。可以送去解压或转码。")

    def _download_path(self):
        if self.last_download is None or not self.last_download.is_file():
            messagebox.showinfo("暂无下载结果", "请先完成下载；如果文件已移动，请直接在相应页选择文件。", parent=self)
            return None
        return self.last_download

    def send_download_to_archive(self):
        path = self._download_path()
        if path is None:
            return
        if swift_extract.detect_format(str(path)) == "unknown":
            messagebox.showinfo("无法识别为压缩包", "这个文件不是可识别的压缩包。文本文件可以送到转码页。", parent=self)
            return
        self.archive.add_paths([str(path)])
        self.tabs.select(self.archive_tab)
        self.status_var.set(f"已添加压缩包：{path.name}。检查输出目录后，点击“开始解压”。")

    def send_download_to_encoder(self):
        path = self._download_path()
        if path is not None:
            self.send_to_encoder([path])

    def send_to_encoder(self, paths):
        worker = self.encoder._worker
        if worker is not None and worker.is_alive():
            messagebox.showinfo("转码正在进行", "请等当前扫描或转换结束后，再添加下一批文件。", parent=self)
            return
        for path in paths:
            self.encoder._add_input(str(Path(path)))
        # 新输入不能复用上一批扫描结果。
        self.encoder._pairs = []
        self.tabs.select(self.encoder)
        self.status_var.set("已添加到转码页。请先扫描预览，核对编码与输出目录，再开始转换。")

    def open_inputs(self, inputs):
        """拖到启动器上的文件按用途分派；链接只填入下载框。"""
        archives, texts = [], []
        for value in inputs:
            if value.lower().startswith(("https://", "http://")):
                self.download.url_var.set(value)
                self.tabs.select(self.download_tab)
                continue
            path = Path(value)
            if not path.exists():
                continue
            if path.is_file() and swift_extract.detect_format(str(path)) != "unknown":
                archives.append(str(path))
            else:
                texts.append(str(path))
        if archives:
            self.archive.add_paths(archives)
            self.tabs.select(self.archive_tab)
        if texts:
            self.send_to_encoder(texts)

    def busy(self):
        return bool(
            (self.download.job_thread and self.download.job_thread.is_alive())
            or self.archive.busy()
            or (self.encoder._worker and self.encoder._worker.is_alive())
        )

    def request_close(self):
        if self._closing:
            return
        self._closing = True
        self.status_var.set("正在停止任务并保存进度，完成后关闭窗口…")
        self.download.shutdown()
        self.archive.shutdown()
        self.encoder.shutdown()
        self.withdraw()
        self._finish_close()

    def _finish_close(self):
        if self.busy():
            self.after(100, self._finish_close)
        else:
            self.destroy()


def main():
    app = ToolboxApp()
    app.open_inputs(sys.argv[1:])
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        detail = traceback.format_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("文件工具箱启动失败", detail, parent=root)
            root.destroy()
        except Exception:
            print(detail, file=sys.stderr)
        raise SystemExit(1)
