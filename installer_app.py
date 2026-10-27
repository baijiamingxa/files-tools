#!/usr/bin/env python3
"""文件工具箱安装程序：当前用户安装，无需管理员权限。"""
from __future__ import annotations

import argparse
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from installer_core import install_payload, register_installation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extract-only", metavar="DIRECTORY", help="仅校验并解包，不注册、不创建快捷方式")
    args = parser.parse_args()
    payload = Path(__file__).resolve().parent / "payload" / "application.zip"
    if args.extract_only:
        install_payload(payload, args.extract_only)
        return 0
    app = tk.Tk()
    app.title("文件工具箱 · 安装程序")
    app.geometry("700x360")
    app.resizable(False, False)
    icon = Path(__file__).resolve().parent / "assets" / "app-icon-64.png"
    if icon.is_file():
        app._icon = tk.PhotoImage(file=str(icon))
        app.iconphoto(True, app._icon)
    frame = ttk.Frame(app, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="安装文件工具箱", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
    ttk.Label(frame, text="包含文档转换、PDF 页面操作及任务记录；依赖随程序安装。\nWord 转 PDF 优先使用已有 LibreOffice；缺少时可在工具箱中自动配置。", wraplength=650).pack(anchor="w", pady=14)
    path = tk.StringVar(app, value=str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "文件工具箱"))
    row = ttk.Frame(frame)
    row.pack(fill="x")
    entry = ttk.Entry(row, textvariable=path)
    entry.pack(side="left", fill="x", expand=True)
    def browse():
        chosen = filedialog.askdirectory(parent=app)
        if chosen:
            path.set(str(Path(chosen) / "文件工具箱"))
    browse_btn = ttk.Button(row, text="选择位置…", command=browse)
    browse_btn.pack(side="right", padx=(8, 0))
    shortcut = tk.BooleanVar(app, value=True)
    shortcut_btn = ttk.Checkbutton(frame, text="在开始菜单创建快捷方式", variable=shortcut)
    shortcut_btn.pack(anchor="w", pady=10)
    status = tk.StringVar(app, value="选择空目录或原工具箱安装目录，然后开始安装。")
    ttk.Label(frame, textvariable=status, wraplength=650).pack(anchor="w")
    bar = ttk.Progressbar(frame, maximum=100)
    bar.pack(fill="x", pady=10)
    events = queue.Queue()
    busy = [False]
    target = [None]
    def close():
        if busy[0]:
            messagebox.showinfo("正在安装", "请等待安装完成；文件校验或写入期间不能直接关闭。", parent=app)
        else:
            app.destroy()
    def start():
        if target[0] is not None:
            os.startfile(str(target[0] / "文件工具箱.exe"))
            app.destroy()
            return
        destination, with_shortcut = Path(path.get()).expanduser().absolute(), shortcut.get()
        application_lock = None
        if os.name == "nt":
            import msvcrt
            from desktop_support import user_directory
            lock_path = user_directory() / "窗口.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            application_lock = open(lock_path, "a+b")
            application_lock.seek(0, 2)
            if not application_lock.tell():
                application_lock.write(b"0")
                application_lock.flush()
            application_lock.seek(0)
            try:
                msvcrt.locking(application_lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                application_lock.close()
                messagebox.showinfo("请先关闭工具箱", "检测到工具箱正在运行，请先关闭后再安装或升级。", parent=app)
                return
        busy[0] = True
        for widget in (entry, browse_btn, shortcut_btn, install_btn):
            widget.configure(state="disabled")
        def work():
            try:
                data = install_payload(payload, destination, lambda done, total, text: events.put(("progress", (done, total, text))))
                register_installation(destination, data, with_shortcut)
                events.put(("done", destination))
            except Exception as exc:
                events.put(("error", str(exc)))
            finally:
                if application_lock is not None:
                    application_lock.seek(0)
                    msvcrt.locking(application_lock.fileno(), msvcrt.LK_UNLCK, 1)
                    application_lock.close()
        threading.Thread(target=work, daemon=True).start()
        poll()
    def poll():
        while True:
            try:
                event, value = events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                done, total, text = value
                bar.configure(value=done / max(total, 1) * 100)
                status.set(text)
            else:
                busy[0] = False
                if event == "done":
                    target[0] = value
                    status.set("安装完成，可从开始菜单启动，或在系统“已安装的应用”中卸载。")
                    install_btn.configure(text="启动工具箱", state="normal")
                    bar.configure(value=100)
                else:
                    status.set("安装失败：" + value)
                    messagebox.showerror("安装失败", value, parent=app)
                    for widget in (entry, browse_btn, shortcut_btn, install_btn):
                        widget.configure(state="normal")
        if busy[0]:
            app.after(80, poll)
    actions = ttk.Frame(frame)
    actions.pack(fill="x", side="bottom")
    install_btn = ttk.Button(actions, text="开始安装", command=start)
    install_btn.pack(side="left")
    ttk.Button(actions, text="关闭", command=close).pack(side="right")
    app.protocol("WM_DELETE_WINDOW", close)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
