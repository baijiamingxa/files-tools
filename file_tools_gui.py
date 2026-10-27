#!/usr/bin/env python3
"""文本替换、文件校验页；文件读写和哈希计算都在后台完成。"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import file_tools as core
from txt_encoder_core import ENCODING_CHOICES, LABEL_TO_CODEC, format_size


class FilePanel(ttk.Frame):
    def __init__(self, parent, status_callback=None, extensions="all", compact=False):
        super().__init__(parent, padding=10)
        self._notify = status_callback or (lambda value: None)
        self.inputs, self.rows = [], []
        self._worker = None
        self._active = self._closing = False
        self._stop = threading.Event()
        self._events = queue.Queue()
        self._timer = None
        self._states = []
        self.input_var = tk.StringVar(self, value="尚未选择文件")
        self.status_var = tk.StringVar(self, value="选择文件或文件夹后开始。")
        self.recursive_var = tk.BooleanVar(self, value=True)
        self.extensions_var = tk.StringVar(self, value=extensions)
        self.controls = ttk.Frame(self)
        self.controls.pack(fill="x")
        sources = ttk.Frame(self.controls, padding=4) if compact else ttk.LabelFrame(self.controls, text="选择文件", padding=8)
        sources.pack(fill="x")
        bar = ttk.Frame(sources)
        bar.pack(fill="x")
        ttk.Button(bar, text="添加文件…", command=self._files).pack(side="left")
        ttk.Button(bar, text="添加文件夹…", command=self._folder).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=lambda: self.set_inputs([])).pack(side="left")
        ttk.Label(bar, textvariable=self.input_var).pack(side="left", padx=10)
        if compact:
            ttk.Checkbutton(bar, text="包含子文件夹", variable=self.recursive_var).pack(side="right")
        else:
            line = ttk.Frame(sources)
            line.pack(fill="x", pady=(6, 0))
            ttk.Checkbutton(line, text="包含子文件夹", variable=self.recursive_var).pack(side="left")
            ttk.Label(line, text="文件类型：").pack(side="left", padx=(16, 0))
            ttk.Entry(line, textvariable=self.extensions_var, width=30).pack(side="left")
            ttk.Label(line, text="逗号分隔；all 表示全部").pack(side="left", padx=8)
        self.actions = ttk.Frame(self.controls)
        self.results = ttk.Frame(self)
        self.results.pack(fill="both", expand=True, pady=8)
        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(side="bottom", fill="x", before=self.results)
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", pady=(6, 0), before=self.results)
        ttk.Label(bottom, textvariable=self.status_var, wraplength=1000).pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(bottom, text="停止", command=self._stop.set, state="disabled")
        self.cancel_btn.pack(side="right")
        for variable in (self.recursive_var, self.extensions_var):
            variable.trace_add("write", lambda *args: self.invalidate())

    def _files(self):
        chosen = filedialog.askopenfilenames(parent=self, title="选择文件")
        if chosen:
            self.set_inputs([*self.inputs, *chosen])

    def _folder(self):
        chosen = filedialog.askdirectory(parent=self)
        if chosen:
            self.set_inputs([*self.inputs, chosen])

    def set_inputs(self, paths):
        if self.busy():
            return
        self.inputs = list(dict.fromkeys(str(Path(path).absolute()) for path in paths))
        self.input_var.set(f"已选择 {len(self.inputs)} 个文件 / 文件夹")
        self.invalidate()

    def invalidate(self):
        if self.busy():
            return
        self.rows = []
        if hasattr(self, "tree"):
            self.tree.delete(*self.tree.get_children())
        self.status_var.set("输入或规则已改变，请重新扫描。")

    def _paths(self, inputs, recursive, extensions):
        return core.collect_files(inputs, recursive, extensions, self._stop)

    def busy(self):
        return self._active or bool(self._worker and self._worker.is_alive())

    def _lock(self, locked):
        if locked:
            self._states = []
            def walk(parent):
                for widget in parent.winfo_children():
                    try:
                        state = str(widget.cget("state"))
                        self._states.append((widget, state))
                        widget.configure(state="disabled")
                    except tk.TclError:
                        pass
                    walk(widget)
            walk(self.controls)
        else:
            for widget, state in self._states:
                if widget.winfo_exists():
                    widget.configure(state=state)
            self._states = []
        self.cancel_btn.configure(state="normal" if locked else "disabled")

    def _run(self, work, finish, task=None):
        if self.busy() or self._closing:
            return
        self._stop.clear()
        self._active = True
        self._lock(True)
        self.status_var.set("正在后台处理，可随时停止…")
        self.progress.configure(value=0)
        self._finish = finish
        self._last_progress = 0
        if task:
            from task_history import tracked
            name, inputs, describe = task
            work = tracked(getattr(self, "history", None), name, work, inputs, describe, self._stop.is_set)
        def report(done, total, detail):
            now = time.monotonic()
            if now - self._last_progress >= 0.1 or done == total:
                self._last_progress = now
                self._events.put(("progress", (done, total, detail)))
        def worker():
            try:
                self._events.put(("result", work(report)))
            except core.FileToolCancelled as exc:
                self._events.put(("cancel", str(exc)))
            except Exception as exc:
                self._events.put(("error", str(exc)))
            finally:
                self._events.put(("done", None))
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self._poll()

    def _poll(self):
        self._timer = None
        done = False
        while True:
            try:
                event, value = self._events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                number, total, detail = value
                self.progress.configure(value=number / max(total, 1) * 100)
                self.status_var.set(f"{number} / {total} · {detail}")
            elif event == "result":
                self._finish(value)
                self.progress.configure(value=100)
            elif event in ("cancel", "error"):
                self.status_var.set(value)
                if hasattr(self, "_render"):
                    self._render()
                if event == "error" and not self._closing:
                    messagebox.showerror("任务未完成", value, parent=self)
            elif event == "done":
                done = True
                self._active = False
                self._lock(False)
                self._notify(self.status_var.get())
        if not done:
            self._timer = self.after(80, self._poll)

    def shutdown(self):
        self._closing = True
        self._stop.set()

    def _table(self, columns):
        self.tree = ttk.Treeview(self.results, columns=[item[0] for item in columns], show="headings", selectmode="extended")
        for name, title, width in columns:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=60, stretch=name in ("path", "detail"))
        y = ttk.Scrollbar(self.results, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(self.results, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        self.results.rowconfigure(0, weight=1)
        self.results.columnconfigure(0, weight=1)


class ReplacePanel(FilePanel):
    def __init__(self, parent, status_callback=None):
        super().__init__(parent, status_callback, extensions="txt,md,csv,log,ini,cfg,json,xml,srt")
        self.checked = set()
        self.options = None
        rules = ttk.LabelFrame(self.controls, text="文字替换（区分大小写、按原文匹配，可输入多行）", padding=8)
        rules.pack(fill="x", pady=8)
        rules.columnconfigure(0, weight=1)
        rules.columnconfigure(1, weight=1)
        ttk.Label(rules, text="查找内容：").grid(row=0, column=0, sticky="w")
        ttk.Label(rules, text="替换为（留空即删除匹配文字）：").grid(row=0, column=1, sticky="w", padx=8)
        self.find_text = tk.Text(rules, height=3, width=30, wrap="word", undo=True)
        self.replace_text = tk.Text(rules, height=3, width=30, wrap="word", undo=True)
        self.find_text.grid(row=1, column=0, sticky="ew")
        self.replace_text.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        line = ttk.Frame(rules)
        line.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.encoding_var = tk.StringVar(self, value="自动检测")
        self.inplace_var = tk.BooleanVar(self, value=False)
        ttk.Label(line, text="源编码：").pack(side="left")
        ttk.Combobox(line, textvariable=self.encoding_var, values=[label for label, _ in ENCODING_CHOICES], state="readonly", width=26).pack(side="left")
        ttk.Checkbutton(line, text="覆盖原文件（先创建备份）", variable=self.inplace_var).pack(side="left", padx=16)
        ttk.Label(line, text="默认生成“原名_替换”副本；保留编码、BOM 和换行。").pack(side="left")
        self.actions.pack(fill="x", pady=(0, 4))
        self.scan_btn = ttk.Button(self.actions, text="1. 扫描预览", command=self.scan)
        self.scan_btn.pack(side="left")
        self.execute_btn = ttk.Button(self.actions, text="2. 执行勾选项…", command=self.execute)
        self.execute_btn.pack(side="left", padx=6)
        ttk.Button(self.actions, text="勾选全部命中", command=self.select_matches).pack(side="left")
        ttk.Button(self.actions, text="取消全部勾选", command=self.clear_checks).pack(side="left", padx=6)
        self._table((("check", "勾选", 60), ("path", "源文件", 300), ("encoding", "编码", 95), ("count", "命中次数", 80), ("state", "状态", 100), ("detail", "预览 / 错误", 390)))
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", self._toggle_selection)
        self.tree.bind("<<TreeviewSelect>>", self._detail)
        self.preview_var = tk.StringVar(self, value="选择结果行可查看预览、目标和备份路径。")
        ttk.Label(self, textvariable=self.preview_var, wraplength=1080).pack(fill="x", before=self.progress, pady=(0, 6))
        for variable in (self.encoding_var, self.inplace_var):
            variable.trace_add("write", lambda *args: self.invalidate())
        for widget in (self.find_text, self.replace_text):
            widget.bind("<<Modified>>", self._text_changed)

    def _text_changed(self, event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self.invalidate()

    def _replace_options(self):
        return core.ReplaceOptions(self.find_text.get("1.0", "end-1c"), self.replace_text.get("1.0", "end-1c"), LABEL_TO_CODEC.get(self.encoding_var.get(), self.encoding_var.get()), self.inplace_var.get())

    def scan(self):
        if self.busy():
            return
        try:
            options = self._replace_options()
            options.validate()
            if not self.inputs:
                raise core.FileToolError("请先选择文件或文件夹。")
        except Exception as exc:
            messagebox.showerror("无法扫描", str(exc), parent=self)
            return
        self.rows, self.checked, self.options = [], set(), options
        self._render()
        inputs, recursive, extensions = list(self.inputs), self.recursive_var.get(), self.extensions_var.get()
        def work(report):
            paths = self._paths(inputs, recursive, extensions)
            return core.preview_replacements(paths, options, self._stop, report)
        def finish(rows):
            self.rows = rows
            self.checked = {index for index, row in enumerate(rows) if row.count and not row.error}
            self._render()
            self.status_var.set(f"共 {len(rows)} 个文件，{len(self.checked)} 个可替换；请核对命中内容后执行。")
        self._run(work, finish)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(index), values=("☑" if index in self.checked else "☐", str(row.path), row.encoding, row.count, row.status, row.error or row.preview))

    def _click(self, event):
        item = self.tree.identify_row(event.y)
        if item and self.tree.identify_column(event.x) == "#1":
            self._toggle(int(item))
            return "break"

    def _toggle_selection(self, event):
        for item in self.tree.selection():
            self._toggle(int(item))
        return "break"

    def _toggle(self, index):
        if self.busy() or not self.rows[index].count or self.rows[index].error or self.rows[index].status != "待替换":
            return
        if index in self.checked:
            self.checked.remove(index)
        else:
            self.checked.add(index)
        self.tree.set(str(index), "check", "☑" if index in self.checked else "☐")

    def _detail(self, event=None):
        selected = self.tree.selection()
        if selected:
            row = self.rows[int(selected[0])]
            self.preview_var.set(f"目标：{row.destination}\n{row.error or row.preview}" + (f"\n备份：{row.backup}" if row.backup else ""))

    def select_matches(self):
        if not self.busy():
            self.checked = {index for index, row in enumerate(self.rows) if row.count and not row.error and row.status == "待替换"}
            self._render()

    def clear_checks(self):
        if not self.busy():
            self.checked.clear()
            self._render()

    def execute(self):
        if self.busy():
            return
        if not self.options or self.options != self._replace_options():
            self.invalidate()
            messagebox.showinfo("需要重新预览", "规则已改变，请重新扫描预览。", parent=self)
            return
        selected = [self.rows[index] for index in sorted(self.checked) if index < len(self.rows) and self.rows[index].status == "待替换" and not self.rows[index].error]
        if not selected:
            messagebox.showinfo("没有勾选项", "请先预览并勾选需要替换的文件。", parent=self)
            return
        mode = "覆盖原文件，每个文件先保存独立备份" if self.options.inplace else "生成 _替换 副本，保留原文件"
        listing = "\n".join(str(row.path) for row in selected[:20])
        if len(selected) > 20:
            listing += f"\n… 共 {len(selected)} 个文件"
        if not messagebox.askyesno("确认批量文本替换", f"{mode}。\n将替换 {len(selected)} 个文件中的 {sum(row.count for row in selected)} 处匹配。\n\n{listing}\n\n确认执行？", parent=self):
            return
        options = self.options
        def finish(rows):
            self.checked.clear()
            self._render()
            self.status_var.set(f"替换完成：成功 {sum(row.status == '已替换' for row in selected)}，失败 {sum(row.status == '失败' for row in selected)}。")
        from task_history import file_rows
        self._run(lambda report: core.execute_replacements(selected, options, self._stop, report), finish,
                  task=("批量文本替换", [row.path for row in selected], lambda result: file_rows(selected)))


class ChecksumPanel(FilePanel):
    def __init__(self, parent, status_callback=None):
        super().__init__(parent, status_callback)
        self.md5_var = tk.BooleanVar(self, value=True)
        self.sha256_var = tk.BooleanVar(self, value=True)
        self.base_var = tk.StringVar(self)
        self.comparisons = []
        line = ttk.Frame(self.controls)
        line.pack(fill="x", pady=8)
        ttk.Checkbutton(line, text="MD5", variable=self.md5_var).pack(side="left")
        ttk.Checkbutton(line, text="SHA-256", variable=self.sha256_var).pack(side="left", padx=8)
        ttk.Label(line, text="校验清单的相对路径基准（留空为清单所在目录）：").pack(side="left", padx=10)
        ttk.Entry(line, textvariable=self.base_var, width=35).pack(side="left", fill="x", expand=True)
        self.actions.pack(fill="x")
        self.calculate_btn = ttk.Button(self.actions, text="计算校验值", command=self.calculate)
        self.calculate_btn.pack(side="left")
        ttk.Button(self.actions, text="导出 CSV…", command=self.export).pack(side="left", padx=6)
        ttk.Button(self.actions, text="导入校验文件对比…", command=self.verify).pack(side="left")
        ttk.Button(self.actions, text="查看对比明细…", command=self.show_comparisons).pack(side="left", padx=6)
        self._table((("path", "文件", 300), ("size", "大小", 80), ("state", "状态", 180), ("md5", "MD5", 270), ("sha256", "SHA-256", 500)))
        self.tree.bind("<<TreeviewSelect>>", self._detail)
        self.detail_var = tk.StringVar(self, value="支持本工具 CSV，以及常见 .md5 / .sha256 清单。只比较已选择并计算的文件。")
        ttk.Label(self, textvariable=self.detail_var, wraplength=1080).pack(fill="x", before=self.progress, pady=(0, 6))
        for variable in (self.md5_var, self.sha256_var):
            variable.trace_add("write", lambda *args: self.invalidate())

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(index), values=(str(row.path), format_size(row.size), row.error or row.status, row.digests.get("md5", ""), row.digests.get("sha256", "")))

    def _detail(self, event=None):
        selected = self.tree.selection()
        if selected:
            row = self.rows[int(selected[0])]
            self.detail_var.set(str(row.path) + "\n" + (row.error or " · ".join(f"{name.upper()}: {value}" for name, value in row.digests.items())))

    def calculate(self):
        if self.busy():
            return
        algorithms = tuple(name for name, enabled in (("md5", self.md5_var.get()), ("sha256", self.sha256_var.get())) if enabled)
        if not self.inputs or not algorithms:
            messagebox.showinfo("无法计算", "请先选择文件，并至少勾选一种算法。", parent=self)
            return
        self.rows, self.comparisons = [], []
        self._render()
        inputs, recursive, extensions = list(self.inputs), self.recursive_var.get(), self.extensions_var.get()
        def work(report):
            return core.calculate_hashes(self._paths(inputs, recursive, extensions), algorithms, self._stop, report)
        def finish(rows):
            self.rows = rows
            self._render()
            self.status_var.set(f"计算完成：共 {len(rows)} 个文件，失败 {sum(bool(row.error) for row in rows)} 个。")
        from task_history import file_rows
        self._run(work, finish, task=("文件校验 · 计算", inputs, file_rows))

    def export(self, path=None):
        if self.busy() or not self.rows:
            return
        if path is None:
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                core.export_hashes(self.rows, path)
                self.status_var.set(f"已导出 {len(self.rows)} 条校验记录：{path}")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=self)

    def verify(self, path=None):
        if self.busy():
            return
        if not self.rows:
            messagebox.showinfo("请先计算", "先选择需要校验的文件并计算校验值，再导入清单。", parent=self)
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="导入校验清单", filetypes=[("校验清单", "*.csv *.md5 *.sha256 *.txt"), ("全部文件", "*")])
        if not path:
            return
        base = self.base_var.get().strip() or str(Path(path).parent)
        rows = list(self.rows)
        def work(report):
            # 重新计算被校验文件，防止把过期的缓存校验值当作当前文件的结果。
            algorithms = tuple(sorted({algorithm for row in rows for algorithm in row.digests}))
            fresh = core.calculate_hashes([row.path for row in rows], algorithms, self._stop, report)
            return fresh, core.compare_hashes(fresh, core.read_checksum_manifest(path), base)
        def finish(result):
            self.rows, self.comparisons = result
            grouped = {}
            for name, algorithm, state, expected, actual in self.comparisons:
                target = Path(name) if Path(name).is_absolute() else Path(base) / name.replace("\\", "/")
                grouped.setdefault(core.key(target), []).append(f"{algorithm}: {state}")
            for row in self.rows:
                row.status = " / ".join(grouped.get(core.key(row.path), ["清单未列出"]))
            self._render()
            good = sum(item[2] == "一致" for item in self.comparisons)
            self.status_var.set(f"对比完成：{good} 项一致，{len(self.comparisons) - good} 项不一致或无法验证。")
            self.detail_var.set("\n".join(f"{name} · {algorithm}：{state}" for name, algorithm, state, _, _ in self.comparisons[:5]))
        from task_history import item
        def describe(result):
            if result is None:
                return []
            return [item(name, state="一致" if state == "一致" else "不一致", detail=f"{algorithm}：{state}，预期 {expected}，实际 {actual}") for name, algorithm, state, expected, actual in result[1]]
        self._run(work, finish, task=("文件校验 · 清单比对", [path], describe))

    def show_comparisons(self):
        if not self.comparisons:
            messagebox.showinfo("暂无对比结果", "请先计算文件校验值，再导入校验清单。", parent=self)
            return
        win = tk.Toplevel(self)
        win.title("文件校验 · 完整对比明细")
        win.geometry("860x520")
        win.transient(self.winfo_toplevel())
        text = tk.Text(win, wrap="word", font=("Microsoft YaHei UI", 10))
        scroll = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        for name, algorithm, state, expected, actual in self.comparisons:
            text.insert("end", f"{name}\n{algorithm.upper()}：{state}\n预期：{expected}\n实际：{actual or '未获得'}\n\n")
        text.configure(state="disabled")
