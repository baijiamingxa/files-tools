#!/usr/bin/env python3
"""静默后台版本检查；只提醒，由用户主动打开发布页。"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

from desktop_support import APP_VERSION, RELEASES_URL, check_release, user_directory


class UpdateController:
    def __init__(self, app, parent, config_path=None):
        self.app = app
        self.path = Path(config_path) if config_path else user_directory() / "更新设置.json"
        self.worker = None
        self._active = self._closing = False
        self.events = queue.Queue()
        self.result = None
        self.window = None
        self.message = tk.StringVar(app, value=f"当前版本：{APP_VERSION}；尚未检查更新。")
        self.enabled = tk.BooleanVar(app, value=True)
        self.last_checked = 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.enabled.set(bool(data.get("auto_check", True)))
            self.last_checked = float(data.get("last_checked", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.button = ttk.Button(parent, text=f"v{APP_VERSION} · 检查更新", command=self.show)
        self.button.pack(side="right", padx=8)

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"auto_check": self.enabled.get(), "last_checked": self.last_checked}), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            pass

    def automatic(self):
        if self.enabled.get() and time.time() - self.last_checked >= 86400:
            self.start()

    def busy(self):
        return self._active or bool(self.worker and self.worker.is_alive())

    def start(self):
        if self.busy() or self._closing:
            return
        self._active = True
        self.message.set("正在检查 GitHub 正式版本…")
        def work():
            try:
                self.events.put((True, check_release()))
            except Exception as exc:
                self.events.put((False, str(exc)))
        self.worker = threading.Thread(target=work, daemon=True, name="检查新版本")
        self.worker.start()
        self._poll()

    def _poll(self):
        try:
            ok, value = self.events.get_nowait()
        except queue.Empty:
            self.app.after(100, self._poll)
            return
        self._active = False
        if ok:
            self.result = value
            self.last_checked = time.time()
            self._save()
            self.message.set(f"发现新版本 {value['version']}，当前为 {APP_VERSION}。" if value["new"] else f"当前 {APP_VERSION}，暂无更新的正式版本。")
            if value["new"]:
                self.button.configure(text=f"新版本 {value['version']} 可用")
            self._render()
        else:
            self.message.set("更新检查失败，可稍后重试：" + value)

    def _render(self):
        if self.window and self.window.winfo_exists():
            self.notes.configure(state="normal")
            self.notes.delete("1.0", "end")
            self.notes.insert("end", self.result["notes"] if self.result else "点击“立即检查”获取正式版本信息。")
            self.notes.configure(state="disabled")

    def show(self):
        if self.window and self.window.winfo_exists():
            self.window.lift()
            return
        self.window = win = tk.Toplevel(self.app)
        win.title("文件工具箱 · 版本更新")
        win.geometry("680x450")
        win.transient(self.app)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"文件工具箱 {APP_VERSION}", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.message, wraplength=630).pack(anchor="w", pady=12)
        ttk.Checkbutton(frame, text="启动时后台检查更新（每天最多一次）", variable=self.enabled, command=self._save).pack(anchor="w")
        self.notes = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.notes.pack(fill="both", expand=True, pady=12)
        self._render()
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(actions, text="立即检查", command=self.start).pack(side="left")
        ttk.Button(actions, text="打开 GitHub 发布页", command=lambda: webbrowser.open(self.result["url"] if self.result else RELEASES_URL)).pack(side="left", padx=8)
        ttk.Button(actions, text="关闭", command=win.destroy).pack(side="right")

    def shutdown(self):
        self._closing = True
