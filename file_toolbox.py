#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件工具箱：在同一窗口下载、压缩解压和转换文本编码。"""

from __future__ import annotations

import os
import sys
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

import c_cleaner
import direct_downloader
import swift_extract
from c_cleaner import CleanerPanel
from batch_rename_gui import RenamePanel
from storage_gui import StoragePanel
from runtime_gui import RuntimeController
from txt_encoder_gui import EncoderPanel
from windows_integration import ARCHIVE_EXTENSIONS


class ToolboxApp(tk.Tk):
    def __init__(self, config_path=None, startup_check=True):
        if os.name == "nt":
            import ctypes
            # 源码运行时也以工具箱自己的身份分组任务栏图标。
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TxtEncoder.FileToolbox")
        super().__init__()
        self._set_app_icon()
        self.title("文件工具箱 · 下载 / 压缩解压 / 转码 / 清理 / 重命名 / 空间分析 / 重复文件")
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
        ttk.Label(header, text="下载、压缩解压、转码、重命名与空间整理").pack(side="left", padx=20)
        self.file_menu_btn = ttk.Menubutton(header, text="文件右键菜单")
        self.file_menu_btn.pack(side="right")
        self.file_menu = tk.Menu(self.file_menu_btn, tearoff=False)
        self.file_menu_btn.configure(menu=self.file_menu)
        for label, action in (("安装 / 更新右键菜单", "install"), ("移除右键菜单", "uninstall"), ("查看安装状态", "status")):
            self.file_menu.add_command(label=label, command=lambda value=action: self._manage_file_menu(value))
        if os.name != "nt":
            self.file_menu_btn.configure(state="disabled")

        self.runtime = RuntimeController(self, self, on_ready=lambda: self.archive.refresh_engines())
        self.status_var = tk.StringVar(self, value="就绪。结果传到下一页后，由你检查并开始处理。")
        ttk.Label(self, textvariable=self.status_var, padding=(12, 6)).pack(side="bottom", fill="x")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=8)
        self.download_tab = ttk.Frame(self.tabs)
        self.archive_tab = ttk.Frame(self.tabs)
        self.encoder = EncoderPanel(self.tabs)
        self.cleaner = CleanerPanel(self.tabs, status_callback=self._forward_status)
        self.renamer = RenamePanel(self.tabs, status_callback=self._forward_status)
        self.space = StoragePanel(self.tabs, mode="space", status_callback=self._forward_status, duplicate_callback=self._find_duplicates_here)
        self.duplicates = StoragePanel(self.tabs, mode="duplicates", status_callback=self._forward_status)
        self.tabs.add(self.download_tab, text="  直链下载  ")
        self.tabs.add(self.archive_tab, text="  压缩 / 解压  ")
        self.tabs.add(self.encoder, text="  文本转码  ")
        self.tabs.add(self.cleaner.frame, text="  C 盘清理  ")
        self.tabs.add(self.renamer, text="  批量重命名  ")
        self.tabs.add(self.space, text="  空间分析  ")
        self.tabs.add(self.duplicates, text="  重复文件  ")

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
        if startup_check:
            self.after(350, self.runtime.start)
        else:
            self.runtime.message.set("运行环境：自动检测已停用（测试模式）。")

    def _set_app_icon(self):
        assets = Path(__file__).resolve().parent / "assets"
        self._icon_images = []
        for size in (64, 32):
            path = assets / f"app-icon-{size}.png"
            if path.is_file():
                self._icon_images.append(tk.PhotoImage(master=self, file=str(path)))
        if self._icon_images:
            # Tk 的 ICO 读取器对 PNG 压缩帧支持不完整；窗口直接使用透明 PNG。
            # EXE 文件和资源管理器仍使用打包时嵌入的多尺寸 ICO。
            self.iconphoto(True, *self._icon_images)

    def _forward_status(self, text):
        self.status_var.set(text)

    def _find_duplicates_here(self, path):
        if self.duplicates.busy() or self.duplicates._active:
            messagebox.showinfo("查重正在进行", "请等当前查重结束后再切换范围。", parent=self)
            return
        self.duplicates.set_root(path)
        self.tabs.select(self.duplicates)
        self.duplicates.start_scan()

    def _download_completed(self, path):
        self.last_download = Path(path)
        self.extract_download_btn.configure(state="normal")
        self.encode_download_btn.configure(state="normal")
        self.status_var.set(f"下载完成：{path}。可以送去解压或转码。")

    def _manage_file_menu(self, action):
        import windows_integration as integration
        try:
            if action == "install":
                count = integration.install()
                text = (f"已为当前用户启用 {count} 种文件类型。\n\n"
                        "右键 TXT 或压缩包，选择“通过文件工具箱打开”。\n"
                        "Windows 11 可在“显示更多选项”中查找。")
            elif action == "uninstall":
                integration.uninstall()
                text = "已移除文件工具箱的右键菜单和打开方式。"
            else:
                text = "已安装，指向当前工具箱目录。" if integration.is_installed() else "尚未安装，或需要更新工具箱路径。"
            messagebox.showinfo("文件右键菜单", text, parent=self)
        except OSError as exc:
            messagebox.showerror("设置右键菜单失败", str(exc), parent=self)

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
        """启动器和右键传入的文件按用途分派；链接只填入下载框。"""
        archives, texts = [], []
        for value in inputs:
            value = str(value)
            if value.lower().startswith(("https://", "http://")):
                self.download.url_var.set(value)
                self.tabs.select(self.download_tab)
                continue
            path = Path(value)
            if not path.exists():
                continue
            if path.is_file() and path.suffix.lower() != ".txt" and (
                path.suffix.lower() in ARCHIVE_EXTENSIONS
                or swift_extract.detect_format(str(path)) != "unknown"
            ):
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
            or self.cleaner.busy()
            or self.renamer.busy()
            or self.space.busy()
            or self.duplicates.busy()
            or self.runtime.busy()
        )

    def request_close(self):
        if self._closing:
            return
        self._closing = True
        self.status_var.set("正在停止任务并保存进度，完成后关闭窗口…")
        self.download.shutdown()
        self.archive.shutdown()
        self.encoder.shutdown()
        self.cleaner.shutdown()
        self.renamer.shutdown()
        self.space.shutdown()
        self.duplicates.shutdown()
        self.runtime.shutdown()
        self.withdraw()
        self._finish_close()

    def _finish_close(self):
        if self.busy():
            self.after(100, self._finish_close)
        else:
            self.destroy()


def main():
    if c_cleaner.ADMIN_TASK_FLAG in sys.argv[1:]:
        # 管理员清理任务：由 UAC 提权启动的独立进程走这条路径，不打开窗口。
        return c_cleaner.run_admin_task_entry(sys.argv[1:])
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
