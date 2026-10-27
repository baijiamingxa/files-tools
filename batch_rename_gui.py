#!/usr/bin/env python3
"""批量重命名页签。扫描和文件操作在线程中执行，Tk 只在主线程更新。"""

from __future__ import annotations

import csv
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from batch_rename import RenameOptions, build_plan, collect_files, execute_plan, path_key

MODES = {"保留原文件名": "keep", "统一名称": "fixed", "文件修改日期": "date"}
DATES = {"年月日_时分秒": "%Y%m%d_%H%M%S", "年月日": "%Y%m%d", "年-月-日_时-分-秒": "%Y-%m-%d_%H-%M-%S"}


class RenamePanel(ttk.Frame):
    def __init__(self, master, status_callback=None, record_dir=None):
        super().__init__(master, padding=10)
        self._app_status = status_callback or (lambda text: None)
        self.record_dir = Path(record_dir) if record_dir else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "文件工具箱" / "重命名记录"
        self.inputs = []
        self.rows = []
        self.undo_rows = []
        self._worker = None
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._closing = False
        self._timer = None
        self._saved_states = []
        defaults = dict(mode="保留原文件名", base_name="", date="年月日_时分秒", find="", replacement="", prefix="", suffix="", start="1", step="1", width="3", position="后置", separator="_", extension="", extensions="all")
        self.vars = {name: tk.StringVar(self, value=value) for name, value in defaults.items()}
        self.recursive_var = tk.BooleanVar(self, value=False)
        self.numbering_var = tk.BooleanVar(self, value=False)
        self.input_var = tk.StringVar(self, value="尚未选择文件或文件夹")
        self.status_var = tk.StringVar(self, value="选择文件，设置规则后先扫描预览；存在冲突时不会执行。")
        self._build()
        for variable in [*self.vars.values(), self.recursive_var, self.numbering_var]:
            variable.trace_add("write", self._invalidate)

    def _build(self):
        self.sources = ttk.LabelFrame(self, text="1. 选择文件", padding=10)
        self.sources.pack(fill="x")
        bar = ttk.Frame(self.sources)
        bar.pack(fill="x")
        ttk.Button(bar, text="添加文件…", command=self._pick_files).pack(side="left")
        ttk.Button(bar, text="添加文件夹…", command=self._pick_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=lambda: self.set_inputs([])).pack(side="left")
        ttk.Label(bar, textvariable=self.input_var).pack(side="left", padx=10)
        filters = ttk.Frame(self.sources)
        filters.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(filters, text="包含子文件夹", variable=self.recursive_var).pack(side="left")
        ttk.Label(filters, text="文件类型：").pack(side="left", padx=(20, 0))
        ttk.Entry(filters, textvariable=self.vars["extensions"], width=24).pack(side="left")
        ttk.Label(filters, text="逗号分隔；all 表示全部文件。按路径、文件名排序编号。").pack(side="left", padx=8)

        self.rules = ttk.LabelFrame(self, text="2. 设置规则（可组合使用）", padding=10)
        self.rules.pack(fill="x", pady=8)
        self.rules.columnconfigure(1, weight=1)
        self.rules.columnconfigure(3, weight=1)
        def field(row, column, label, name, values=None):
            ttk.Label(self.rules, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=3)
            if values is None:
                widget = ttk.Entry(self.rules, textvariable=self.vars[name])
            else:
                widget = ttk.Combobox(self.rules, textvariable=self.vars[name], values=list(values), state="readonly")
            widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 18), pady=3)
        field(0, 0, "基础名称：", "mode", MODES)
        field(0, 2, "统一名称：", "base_name")
        field(1, 0, "查找文字：", "find")
        field(1, 2, "替换为：", "replacement")
        field(2, 0, "名称前缀：", "prefix")
        field(2, 2, "名称后缀：", "suffix")
        field(3, 0, "新扩展名：", "extension")
        field(3, 2, "修改日期格式：", "date", DATES)
        ttk.Checkbutton(self.rules, text="添加序号", variable=self.numbering_var).grid(row=4, column=0, sticky="w", pady=3)
        numbers = ttk.Frame(self.rules)
        numbers.grid(row=4, column=1, sticky="w")
        for label, name in (("起始", "start"), ("步长", "step"), ("位数", "width")):
            ttk.Label(numbers, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(numbers, textvariable=self.vars[name], width=5).pack(side="left", padx=(0, 8))
        field(4, 2, "序号位置：", "position", ("后置", "前置"))
        field(5, 0, "序号分隔符：", "separator")
        ttk.Label(self.rules, text="扩展名留空保留原样；替换只作用于基础名称。仅改文件名，保留目录和文件内容。").grid(row=5, column=2, columnspan=2, sticky="w", pady=3)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(0, 8))
        self.preview_btn = ttk.Button(actions, text="① 扫描预览", command=self.start_preview)
        self.preview_btn.pack(side="left")
        self.execute_btn = ttk.Button(actions, text="② 确认并执行", command=self.start_execute, state="disabled")
        self.execute_btn.pack(side="left", padx=8)
        self.undo_btn = ttk.Button(actions, text="撤销上次", command=self.start_undo, state="disabled")
        self.undo_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止并回退", command=self._stop.set, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.export_btn = ttk.Button(actions, text="导出表格…", command=self.export_csv)
        self.export_btn.pack(side="left")
        self.progress = ttk.Progressbar(actions, length=160)
        self.progress.pack(side="right")
        ttk.Label(self, textvariable=self.status_var, wraplength=1050).pack(fill="x", pady=(0, 6))
        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("folder", "old", "new", "status"), show="headings")
        for name, title, width in (("folder", "所在文件夹", 230), ("old", "原文件名", 220), ("new", "新文件名", 240), ("status", "检查结果", 280)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=100)
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("error", foreground="#b42318")
        self.tree.tag_configure("changed", foreground="#166534")

    def busy(self):
        return self._worker is not None and self._worker.is_alive()

    def shutdown(self):
        self._closing = True
        self._stop.set()
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None

    def _invalidate(self, *_):
        self.rows = []
        self.execute_btn.configure(state="disabled")
        self.status_var.set("输入或规则已变更，请重新扫描预览。")

    def set_inputs(self, paths):
        if self.busy():
            return
        self.inputs = list(dict.fromkeys(Path(p) for p in paths))
        summary = str(self.inputs[0]) if len(self.inputs) == 1 else f"已选择 {len(self.inputs)} 个文件或文件夹"
        self.input_var.set(summary if self.inputs else "尚未选择文件或文件夹")
        self._invalidate()
        self.tree.delete(*self.tree.get_children())

    def _pick_files(self):
        paths = filedialog.askopenfilenames(parent=self, title="选择要重命名的文件")
        if paths:
            self.set_inputs([*self.inputs, *paths])

    def _pick_folder(self):
        path = filedialog.askdirectory(parent=self, title="选择包含待重命名文件的文件夹")
        if path:
            self.set_inputs([*self.inputs, path])

    def _make_options(self):
        values = {name: variable.get() for name, variable in self.vars.items()}
        return RenameOptions(
            mode=MODES[values["mode"]], base_name=values["base_name"], date_format=DATES[values["date"]],
            find=values["find"], replacement=values["replacement"], prefix=values["prefix"], suffix=values["suffix"],
            extension=values["extension"], numbering=self.numbering_var.get(), start=int(values["start"]),
            step=int(values["step"]), width=int(values["width"]), number_position="prefix" if values["position"] == "前置" else "suffix", separator=values["separator"],
        )

    def _set_busy(self, busy):
        if busy:
            def disable(parent):
                for widget in parent.winfo_children():
                    if isinstance(widget, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
                        self._saved_states.append((widget, str(widget.cget("state"))))
                        widget.configure(state="disabled")
                    disable(widget)
            disable(self.sources)
            disable(self.rules)
        else:
            for widget, state in self._saved_states:
                widget.configure(state=state)
            self._saved_states.clear()
        for button in (self.preview_btn, self.export_btn):
            button.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.execute_btn.configure(state="normal" if not busy and self.rows and any(r.changed for r in self.rows) and not any(r.error for r in self.rows) else "disabled")
        self.undo_btn.configure(state="normal" if not busy and self.undo_rows else "disabled")

    def _launch(self, action, work):
        if self.busy():
            return
        self._stop.clear()
        self._set_busy(True)
        self.status_var.set("正在扫描预览…" if action == "preview" else "正在执行，请等待完成或回退…")
        self.progress.configure(value=0)
        def run():
            try:
                self._queue.put((action, work()))
            except Exception as exc:
                self._queue.put(("error", str(exc)))
        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()
        self._timer = self.after(60, self._poll)

    def start_preview(self):
        if not self.inputs:
            messagebox.showinfo("选择文件", "请先添加文件或文件夹。", parent=self)
            return
        try:
            options = self._make_options()
        except (ValueError, KeyError) as exc:
            messagebox.showerror("规则无效", f"请检查序号和命名规则：{exc}", parent=self)
            return
        inputs, recursive, extensions = list(self.inputs), self.recursive_var.get(), self.vars["extensions"].get()
        self.rows = []
        self._launch("preview", lambda: build_plan(collect_files(inputs, recursive, extensions, self._stop), options, self._stop))

    def _confirm(self, count, undo=False):
        return messagebox.askyesno("确认撤销" if undo else "确认重命名", f"将{'恢复' if undo else '修改'} {count} 个文件的名称。\n请核对预览表中的原名和新名。\n\n是否继续？", parent=self)

    def start_execute(self):
        rows = list(self.rows)
        if not rows or any(row.error for row in rows):
            return
        count = sum(row.changed for row in rows)
        if count and self._confirm(count):
            self._execute(rows, "execute")

    def start_undo(self):
        if self.undo_rows and self._confirm(len(self.undo_rows), undo=True):
            self._execute(list(self.undo_rows), "undo")

    def _execute(self, rows, action):
        self.rows = []
        def progress(done, total):
            if done == total or done % max(1, total // 100) == 0:
                self._queue.put(("progress", (done, total)))
        from task_history import tracked, item
        def describe(result):
            if result is None:
                return []
            return [item(row.src, row.dst, "已完成" if row.changed else "保持原样", "重命名日志：" + str(result.record_path or "")) for row in result.rows]
        work = tracked(getattr(self, "history", None), "撤销重命名" if action == "undo" else "批量重命名",
                       lambda: execute_plan(rows, self.record_dir, self._stop, progress), [row.src for row in rows], describe, self._stop.is_set)
        self._launch(action, work)

    def _show_rows(self, rows, completed=False):
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            text = row.error or ("已重命名" if completed and row.changed else "待重命名" if row.changed else "保持原样")
            self.tree.insert("", "end", values=(str(row.src.parent), row.src.name, row.dst.name, text), tags=("error" if row.error else "changed",))

    def _poll(self):
        self._timer = None
        if self._closing:
            return
        try:
            while True:
                action, value = self._queue.get_nowait()
                if action == "progress":
                    self.progress.configure(maximum=value[1], value=value[0])
                elif action == "preview":
                    self.rows = value
                    self._show_rows(value)
                    errors = sum(bool(row.error) for row in value)
                    self.status_var.set(f"预览完成：{len(value)} 个文件，{sum(row.changed for row in value)} 个需改名，{errors} 个冲突。" + ("请修改规则后重新预览。" if errors else "核对后点击确认并执行。"))
                    self._set_busy(False)
                elif action in ("execute", "undo"):
                    self.undo_rows = value.undo_rows if action == "execute" else []
                    mapping = {path_key(row.src): row.dst for row in value.rows}
                    self.inputs = [mapping.get(path_key(path), path) for path in self.inputs]
                    if len(self.inputs) == 1:
                        self.input_var.set(str(self.inputs[0]))
                    self._show_rows(value.rows, completed=True)
                    self.status_var.set(f"{'撤销' if action == 'undo' else '重命名'}完成：{sum(row.changed for row in value.rows)} 个文件。操作记录：{value.record_path}")
                    self._set_busy(False)
                elif action == "error":
                    self.rows = []
                    self.status_var.set(value)
                    self._set_busy(False)
                    messagebox.showerror("重命名未完成", value, parent=self)
                self._app_status("重命名：" + self.status_var.get())
        except queue.Empty:
            pass
        if self.busy() or not self._queue.empty():
            self._timer = self.after(60, self._poll)

    def export_csv(self):
        if not self.tree.get_children():
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出重命名表格", defaultextension=".csv", initialfile="重命名预览.csv")
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as output:
                    writer = csv.writer(output)
                    writer.writerow(["所在文件夹", "原文件名", "新文件名", "检查结果"])
                    writer.writerows(self.tree.item(item, "values") for item in self.tree.get_children())
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=self)
