#!/usr/bin/env python3
"""空间分析与重复文件页签，共用后台扫描与回收前复查。"""

from __future__ import annotations

import csv
import math
import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import storage_tools as core
from txt_encoder_core import format_size


class StoragePanel(ttk.Frame):
    def __init__(self, master, mode="space", status_callback=None, duplicate_callback=None):
        super().__init__(master, padding=10)
        self.mode = mode
        self._app_status = status_callback or (lambda text: None)
        self._duplicate_callback = duplicate_callback
        self.scan = self.duplicates = None
        self.checked = set()
        self._rows = {}
        self._thread = None
        self._active = self._closing = False
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._timer = None
        self._descending = True
        self._saved_states = []
        self.recycler = None  # 测试可注入临时目录回收器；正常运行使用 Windows Shell。
        self.root_var = tk.StringVar(self)
        self.minimum_var = tk.StringVar(self, value="0")
        self.limit_var = tk.StringVar(self, value="200")
        self.kind_var = tk.StringVar(self, value="全部")
        self.empty_var = tk.BooleanVar(self, value=False)
        self.status_var = tk.StringVar(self, value="选择文件夹或磁盘根目录，再开始扫描。扫描不会修改文件。")
        self.summary_var = tk.StringVar(self, value="")
        self._build()
        self.root_var.trace_add("write", self._invalidate)
        self.minimum_var.trace_add("write", self._invalidate if mode == "duplicates" else self._filter_changed)
        self.empty_var.trace_add("write", self._invalidate)
        self.kind_var.trace_add("write", self._filter_changed)
        self.limit_var.trace_add("write", self._filter_changed)

    def _build(self):
        self.options = ttk.LabelFrame(self, text="选择扫描范围", padding=10)
        self.options.pack(fill="x")
        top = ttk.Frame(self.options)
        top.pack(fill="x")
        ttk.Label(top, text="文件夹 / 磁盘：").pack(side="left")
        ttk.Entry(top, textvariable=self.root_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="选择文件夹…", command=self._browse).pack(side="left")
        filters = ttk.Frame(self.options)
        filters.pack(fill="x", pady=(8, 0))
        ttk.Label(filters, text="最小大小（MB）：").pack(side="left")
        ttk.Entry(filters, textvariable=self.minimum_var, width=9).pack(side="left")
        ttk.Label(filters, text="显示上限：").pack(side="left", padx=(16, 0))
        ttk.Combobox(filters, textvariable=self.limit_var, values=(200, 1000, 5000), width=8, state="readonly").pack(side="left")
        if self.mode == "space":
            ttk.Label(filters, text="类型：").pack(side="left", padx=(16, 0))
            ttk.Combobox(filters, textvariable=self.kind_var, values=("全部", "仅文件", "仅文件夹"), width=10, state="readonly").pack(side="left")
        else:
            ttk.Checkbutton(filters, text="包含空文件", variable=self.empty_var).pack(side="left", padx=16)
        note = "按逻辑大小排序；目录大小包含子目录，表格各行不能直接相加。" if self.mode == "space" else "先按大小分组，再计算完整 SHA-256；勾选表示删除，每组至少保留一份。"
        ttk.Label(self, text=note).pack(anchor="w", pady=8)
        actions = ttk.Frame(self)
        actions.pack(fill="x")
        self.scan_btn = ttk.Button(actions, text="① 扫描空间" if self.mode == "space" else "① 查找重复文件", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止", command=self._stop.set, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(actions, text="打开所在位置", command=self.open_location)
        self.open_btn.pack(side="left")
        if self.mode == "space":
            self.drill_btn = ttk.Button(actions, text="分析所选文件夹", command=self.drill_down)
            self.drill_btn.pack(side="left", padx=6)
            self.duplicates_btn = ttk.Button(actions, text="在此查重", command=self.send_duplicates)
            self.duplicates_btn.pack(side="left")
        else:
            self.suggest_btn = ttk.Button(actions, text="勾选建议副本", command=self.select_suggested)
            self.suggest_btn.pack(side="left", padx=6)
            self.clear_btn = ttk.Button(actions, text="清空勾选", command=self.clear_checked)
            self.clear_btn.pack(side="left")
        self.delete_btn = ttk.Button(actions, text="② 删除所选…" if self.mode == "space" else "② 删除勾选副本…", command=self.start_delete, state="disabled")
        self.delete_btn.pack(side="left", padx=6)
        self.export_btn = ttk.Button(actions, text="导出 CSV…", command=self.export_csv, state="disabled")
        self.export_btn.pack(side="right")
        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(fill="x", pady=(10, 5))
        ttk.Label(self, textvariable=self.summary_var, wraplength=1050).pack(fill="x")
        ttk.Label(self, textvariable=self.status_var, wraplength=1050).pack(fill="x", pady=(4, 8))
        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        columns = (("kind", "类型", 65), ("size", "逻辑大小", 105), ("share", "占扫描目录", 85), ("count", "文件数", 70), ("name", "名称", 225), ("folder", "所在位置", 450)) if self.mode == "space" else (("check", "删除", 50), ("group", "组", 45), ("size", "文件大小", 95), ("name", "文件名", 205), ("modified", "修改时间", 145), ("advice", "建议 / 说明", 170), ("folder", "所在位置", 300))
        self.tree = ttk.Treeview(table, columns=[column[0] for column in columns], show="headings", selectmode="extended")
        for name, title, width in columns:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=40)
        if self.mode == "space":
            self.tree.heading("size", text="逻辑大小 ▼", command=self._sort_size)
        else:
            self.tree.bind("<Button-1>", self._click_check)
            self.tree.bind("<space>", lambda _event: self.toggle_checked(self.tree.selection()))
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())
        self.tree.tag_configure("keeper", foreground="#167047")
        self.tree.tag_configure("folder", foreground="#1759a3")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def shutdown(self):
        self._closing = True
        self._stop.set()
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None

    def set_root(self, path):
        if not self._active:
            self.root_var.set(str(path))

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="选择需要分析的文件夹或磁盘")
        if path:
            self.set_root(path)

    def _invalidate(self, *_):
        self.scan = self.duplicates = None
        self.checked.clear()
        self._rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.summary_var.set("")
        self.status_var.set("扫描范围或查重条件已变更，请重新扫描。")
        self._update_buttons()

    def _minimum(self):
        value = float(self.minimum_var.get() or "0")
        scaled = value * 1024 * 1024
        if not math.isfinite(scaled) or value < 0:
            raise ValueError("最小大小须为非负数")
        return int(scaled)

    def _filter_changed(self, *_):
        if not self._active:
            try:
                self._render()
            except ValueError:
                self.status_var.set("请输入有效的非负大小。")

    def _update_buttons(self):
        ready = self.scan is not None and not self._active
        selected = bool(self.tree.selection()) if self.mode == "space" else bool(self.checked)
        self.delete_btn.configure(state="normal" if ready and selected and os.name == "nt" else "disabled")
        self.export_btn.configure(state="normal" if ready else "disabled")
        self.open_btn.configure(state="normal" if self.tree.selection() and not self._active else "disabled")

    def _set_busy(self, busy):
        self._active = busy
        if busy:
            def disable(parent):
                for widget in parent.winfo_children():
                    if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Button, ttk.Checkbutton)):
                        self._saved_states.append((widget, str(widget.cget("state"))))
                        widget.configure(state="disabled")
                    disable(widget)
            disable(self.options)
        else:
            for widget, state in self._saved_states:
                widget.configure(state=state)
            self._saved_states.clear()
        self.scan_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        for name in ("drill_btn", "duplicates_btn", "suggest_btn", "clear_btn"):
            if hasattr(self, name):
                getattr(self, name).configure(state="disabled" if busy else "normal")
        self._update_buttons()

    def _launch(self, kind, work):
        if self._active:
            return
        self._stop.clear()
        self._set_busy(True)
        self.progress.configure(value=0)
        self.status_var.set("正在扫描…" if kind == "scan" else "正在复查所选文件；系统操作开始后可在系统窗口中取消。")
        def run():
            try:
                self._queue.put((kind, work()))
            except core.ScanCancelled as exc:
                self._queue.put(("cancel", str(exc)))
            except Exception as exc:
                self._queue.put(("error", str(exc)))
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._timer = self.after(80, self._poll)

    def start_scan(self):
        if self._active:
            return
        root = self.root_var.get().strip()
        if not root:
            messagebox.showinfo("选择范围", "请先选择文件夹或输入磁盘根目录。", parent=self)
            return
        try:
            minimum = self._minimum()
        except ValueError as exc:
            messagebox.showerror("大小无效", str(exc), parent=self)
            return
        include_empty = self.empty_var.get()
        self._invalidate()
        def work():
            scan = core.scan_tree(root, self._stop, self._progress)
            return (scan, core.find_duplicates(scan, self._stop, self._progress, include_empty, minimum) if self.mode == "duplicates" else None)
        self._launch("scan", work)

    def _progress(self, phase, done, total, text):
        self._queue.put(("progress", (phase, done, total, text)))

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        if self.scan is None:
            return
        limit, minimum = int(self.limit_var.get()), self._minimum()
        if self.mode == "space":
            entries = list(self.scan.files) + [folder for k, folder in self.scan.folders.items() if k != core.key(self.scan.root)]
            entries = [entry for entry in entries if entry.size >= minimum and (self.kind_var.get() != "仅文件" or isinstance(entry, core.FileInfo)) and (self.kind_var.get() != "仅文件夹" or isinstance(entry, core.FolderInfo))]
            entries.sort(key=lambda entry: (entry.size, core.key(entry.path)), reverse=self._descending)
            for entry in entries[:limit]:
                folder = isinstance(entry, core.FolderInfo)
                share = f"{entry.size / self.scan.total_size:.1%}" if self.scan.total_size else "0%"
                iid = self.tree.insert("", "end", values=("文件夹" if folder else "文件", format_size(entry.size), share, entry.files if folder else 1, entry.path.name, str(entry.path.parent)), tags=("folder",) if folder else ())
                self._rows[iid] = entry
            self.summary_var.set(f"合计逻辑大小 {format_size(self.scan.total_size)}，{len(self.scan.files)} 个文件、{len(self.scan.folders)-1} 个文件夹；显示 {min(limit,len(entries))}/{len(entries)} 行。")
        else:
            all_count = sum(len(group.files) for group in self.duplicates.groups)
            for number, group in enumerate(self.duplicates.groups, 1):
                if len(self._rows) >= limit:
                    break
                keeper = group.keeper
                files = sorted(group.files, key=lambda file: (file is not keeper, core.key(file.path)))
                for file in files:
                    if len(self._rows) >= limit:
                        break
                    advice = "建议保留（最新）" if file is keeper else "可选副本"
                    if file.links > 1:
                        advice += " · 硬链接"
                    iid = self.tree.insert("", "end", values=("☑" if core.key(file.path) in self.checked else "☐", number, format_size(file.size), file.path.name, datetime.fromtimestamp(file.mtime_ns/1e9).strftime("%Y-%m-%d %H:%M:%S"), advice, str(file.path.parent)), tags=("keeper",) if file is keeper else ())
                    self._rows[iid] = (file, group)
            potential = sum(group.potential_size for group in self.duplicates.groups)
            self.summary_var.set(f"{len(self.duplicates.groups)} 组、{all_count} 个重复文件；建议副本潜在大小 {format_size(potential)}。显示 {len(self._rows)}/{all_count} 行；已勾选 {len(self.checked)} 份。")
        self._update_buttons()

    def _sort_size(self):
        self._descending = not self._descending
        self.tree.heading("size", text="逻辑大小 ▼" if self._descending else "逻辑大小 ▲")
        self._render()

    def _click_check(self, event):
        if self.tree.identify_column(event.x) == "#1":
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.selection_set(iid)
                self.toggle_checked([iid])
                return "break"

    def toggle_checked(self, items):
        if self._active or self.mode != "duplicates":
            return
        updated = set(self.checked)
        for iid in items:
            if iid in self._rows:
                file, group = self._rows[iid]
                path = core.key(file.path)
                if path in updated:
                    updated.remove(path)
                elif all(core.key(other.path) in updated or other is file for other in group.files):
                    messagebox.showinfo("保留副本", "每组至少保留一份。可先清空勾选，再选择需要删除的副本。", parent=self)
                    return
                else:
                    updated.add(path)
        self.checked = updated
        self._render()

    def select_suggested(self):
        if not self._active and self.duplicates is not None:
            self.checked = {core.key(file.path) for file, group in self._rows.values() if file is not group.keeper}
            self._render()

    def clear_checked(self):
        if not self._active:
            self.checked.clear()
            self._render()

    def _selected_paths(self):
        if self.mode == "space":
            return [self._rows[iid].path for iid in self.tree.selection() if iid in self._rows]
        return [file.path for group in self.duplicates.groups for file in group.files if core.key(file.path) in self.checked]

    def _confirm_delete(self, entries):
        lines = "\n".join(str(entry.path) for entry in entries[:12])
        if len(entries) > 12:
            lines += f"\n另 {len(entries)-12} 项"
        text = (f"将处理 {len(entries)} 项，合计逻辑大小约 {format_size(sum(entry.size for entry in entries))}：\n\n{lines}\n\n"
                "优先移入 Windows 回收站。若系统提示无法回收，请在系统对话框中核对并决定是否继续。\n"
                "回收站内容清空前，磁盘可用空间不一定增加。是否继续？")
        return messagebox.askyesno("确认文件操作", text, icon="warning", parent=self)

    def start_delete(self):
        if self._active or self.scan is None:
            return
        try:
            paths = self._selected_paths()
            entries = core.selected_entries(self.scan, paths)
        except core.StorageError as exc:
            messagebox.showinfo("选择文件", str(exc), parent=self)
            return
        if self._confirm_delete(entries):
            scan, groups = self.scan, self.duplicates.groups if self.duplicates else None
            self._launch("delete", lambda: core.delete_selected(scan, paths, groups, self._stop, self.recycler))

    def open_location(self):
        selected = self.tree.selection()
        if not selected or self._active:
            return
        entry = self._rows[selected[0]]
        path = entry.path if self.mode == "space" else entry[0].path
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _selected_folder(self):
        selected = self.tree.selection()
        if selected:
            entry = self._rows[selected[0]]
            return entry.path if isinstance(entry, core.FolderInfo) else entry.path.parent
        return Path(self.root_var.get())

    def drill_down(self):
        if not self._active and self.scan is not None:
            self.set_root(self._selected_folder())
            self.start_scan()

    def send_duplicates(self):
        if not self._active and self.scan is not None and self._duplicate_callback:
            self._duplicate_callback(self._selected_folder())

    def _poll(self):
        self._timer = None
        if self._closing:
            return
        try:
            while True:
                kind, value = self._queue.get_nowait()
                if kind == "progress":
                    phase, done, total, text = value
                    if phase == "hash":
                        self.progress.configure(value=done*100/total if total else 0)
                        self.status_var.set(f"SHA-256 校验：{format_size(done)}/{format_size(total)}。")
                    else:
                        self.status_var.set(f"已找到 {done} 个文件，{format_size(total)}；{text}")
                elif kind == "scan":
                    self.scan, self.duplicates = value
                    self._set_busy(False)
                    self.progress.configure(value=100)
                    self._render()
                    errors = len(self.scan.errors) + (len(self.duplicates.errors) if self.duplicates else 0)
                    self.status_var.set(f"扫描完成。无法访问/变化项 {errors}，跳过链接 {self.scan.skipped_links}、系统回收目录 {self.scan.skipped_system}。" + (f"硬链接别名合并 {self.duplicates.hardlink_aliases} 项。" if self.duplicates else ""))
                elif kind == "delete":
                    self._set_busy(False)
                    self._invalidate()
                    self.status_var.set(f"系统操作结束：已移走 {len(value.removed)} 项，未处理 {len(value.remaining)} 项。{value.message}请重新扫描更新结果。")
                    messagebox.showinfo("文件操作结果", self.status_var.get(), parent=self)
                else:
                    self._set_busy(False)
                    if kind == "error":
                        self._invalidate()
                    self.status_var.set(value)
                    if kind == "error":
                        messagebox.showerror("未完成", value, parent=self)
                self._app_status(("空间分析：" if self.mode == "space" else "重复文件：") + self.status_var.get())
        except queue.Empty:
            pass
        if self.busy() or not self._queue.empty():
            self._timer = self.after(80, self._poll)

    def export_csv(self):
        if self.scan is None or self._active:
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出完整扫描结果", defaultextension=".csv", initialfile="空间分析.csv" if self.mode == "space" else "重复文件.csv")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output)
                if self.mode == "space":
                    writer.writerow(["类型", "路径", "逻辑字节数", "文件数"])
                    for entry in [*self.scan.files, *self.scan.folders.values()]:
                        writer.writerow(["文件夹" if isinstance(entry, core.FolderInfo) else "文件", str(entry.path), entry.size, entry.files if isinstance(entry, core.FolderInfo) else 1])
                else:
                    writer.writerow(["组", "路径", "字节数", "修改时间", "SHA256", "建议保留", "勾选删除", "硬链接数"])
                    for number, group in enumerate(self.duplicates.groups, 1):
                        keeper = group.keeper
                        for file in group.files:
                            writer.writerow([number, str(file.path), file.size, datetime.fromtimestamp(file.mtime_ns/1e9).isoformat(), group.digest, file is keeper, core.key(file.path) in self.checked, file.links])
                writer.writerow([])
                writer.writerow(["未能扫描或校验的路径", "原因"])
                for error_path, error in [*self.scan.errors, *(self.duplicates.errors if self.duplicates else [])]:
                    writer.writerow([str(error_path), error])
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
