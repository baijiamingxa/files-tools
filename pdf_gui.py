#!/usr/bin/env python3
"""PDF 页序调整和批处理预览界面。"""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from file_tools_gui import FilePanel
import pdf_tools as core

MODES = {"合并 PDF（按文件顺序）": "merge", "拆分 PDF（每份指定页数）": "split", "提取页面": "extract", "旋转页面": "rotate"}


class PdfPanel(FilePanel):
    def __init__(self, parent, status_callback=None):
        super().__init__(parent, status_callback, extensions="pdf", compact=True)
        self.configure(padding=6)
        self.options = None
        order = ttk.Frame(self.controls, padding=(4, 0, 4, 4))
        order.pack(fill="x", pady=(6, 0))
        self.order_list = tk.Listbox(order, height=2, exportselection=False)
        self.order_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(order, orient="vertical", command=self.order_list.yview)
        scroll.pack(side="left", fill="y")
        self.order_list.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(order)
        buttons.pack(side="left", padx=8)
        ttk.Label(buttons, text="合并按列表顺序；文件夹内按路径排序").pack(anchor="w")
        moves = ttk.Frame(buttons)
        moves.pack(fill="x")
        for label, delta in (("上移", -1), ("下移", 1)):
            ttk.Button(moves, text=label, command=lambda value=delta: self.move_input(value)).pack(side="left", padx=2)
        ttk.Button(moves, text="移除所选", command=self.remove_input).pack(side="left", padx=2)
        self.mode_var = tk.StringVar(self, value=next(iter(MODES)))
        self.pages_var = tk.StringVar(self)
        self.output_var = tk.StringVar(self)
        self.filename_var = tk.StringVar(self, value="合并结果.pdf")
        self.split_var = tk.StringVar(self, value="1")
        self.rotation_var = tk.StringVar(self, value="90")
        self.password_var = tk.StringVar(self)
        settings = ttk.LabelFrame(self.controls, text="PDF 操作", padding=4)
        settings.pack(fill="x", pady=6)
        line = ttk.Frame(settings)
        line.pack(fill="x")
        ttk.Combobox(line, textvariable=self.mode_var, values=list(MODES), width=28, state="readonly").pack(side="left")
        ttk.Label(line, text="PDF 密码（若有）：").pack(side="left", padx=(18, 4))
        ttk.Entry(line, textvariable=self.password_var, show="*", width=18).pack(side="left")
        self.mode_frames = {}
        container = ttk.Frame(settings)
        container.pack(fill="x", pady=4)
        container.columnconfigure(0, weight=1)
        for mode in MODES.values():
            frame = ttk.Frame(container)
            frame.grid(row=0, column=0, sticky="ew")
            self.mode_frames[mode] = frame
            if mode == "merge":
                ttk.Label(frame, text="合并文件名：").pack(side="left")
                ttk.Entry(frame, textvariable=self.filename_var).pack(side="left", fill="x", expand=True)
            elif mode == "split":
                ttk.Label(frame, text="每份页数：").pack(side="left")
                ttk.Entry(frame, textvariable=self.split_var, width=8).pack(side="left")
                ttk.Label(frame, text="最后一份可少于设定页数").pack(side="left", padx=12)
            else:
                ttk.Label(frame, text="页码范围：").pack(side="left")
                ttk.Entry(frame, textvariable=self.pages_var).pack(side="left", fill="x", expand=True)
                if mode == "rotate":
                    ttk.Label(frame, text="顺时针：").pack(side="left", padx=(8, 0))
                    ttk.Combobox(frame, textvariable=self.rotation_var, values=("90", "180", "270"), state="readonly", width=5).pack(side="left")
                ttk.Label(frame, text="例：1-3,5；空白为全部，从 1 起").pack(side="left", padx=8)
        def show_mode(*args):
            selected = MODES[self.mode_var.get()]
            for mode, frame in self.mode_frames.items():
                if mode == selected:
                    frame.grid()
                else:
                    frame.grid_remove()
        self.mode_var.trace_add("write", show_mode)
        show_mode()
        folder = ttk.Frame(settings)
        folder.pack(fill="x")
        ttk.Label(folder, text="输出目录（可留空）：").pack(side="left")
        ttk.Entry(folder, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder, text="浏览…", command=self._output_folder).pack(side="left", padx=(8, 0))
        ttk.Label(settings, text="默认存入源目录的“PDF处理结果”；不覆盖。密码仅用于读取，输出不加密、不保留数字签名效力。").pack(anchor="w", pady=(4, 0))
        self.actions.pack(fill="x")
        ttk.Button(self.actions, text="1. 扫描预览", command=self.scan).pack(side="left")
        ttk.Button(self.actions, text="2. 确认执行…", command=self.execute).pack(side="left", padx=6)
        ttk.Button(self.actions, text="打开输出目录", command=self.open_output).pack(side="left")
        self._table((("path", "输入文件 / 合并顺序", 300), ("pages", "源页数", 75), ("output", "输出位置", 260), ("state", "状态", 80), ("detail", "说明 / 错误", 320)))
        self.tree.bind("<<TreeviewSelect>>", self._detail)
        self.detail_var = tk.StringVar(self, value="预览后选择任务可查看完整输出列表；执行后自动保存任务记录。")
        ttk.Label(self, textvariable=self.detail_var, wraplength=1000).pack(fill="x", before=self.results)
        for variable in (self.mode_var, self.pages_var, self.output_var, self.filename_var, self.split_var, self.rotation_var, self.password_var):
            variable.trace_add("write", lambda *args: self.invalidate())

    def set_inputs(self, paths):
        if self.busy():
            return
        super().set_inputs(paths)
        self.order_list.delete(0, "end")
        for path in self.inputs:
            self.order_list.insert("end", path)

    def move_input(self, delta):
        selected = self.order_list.curselection()
        if self.busy() or not selected:
            return
        index, paths = selected[0], list(self.inputs)
        target = index + delta
        if 0 <= target < len(paths):
            paths[index], paths[target] = paths[target], paths[index]
            self.set_inputs(paths)
            self.order_list.selection_set(target)

    def remove_input(self):
        selected = set(self.order_list.curselection())
        if not self.busy():
            self.set_inputs([path for index, path in enumerate(self.inputs) if index not in selected])

    def _output_folder(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.output_var.set(path)

    def _pdf_options(self):
        mode = MODES[self.mode_var.get()]
        options = core.PdfOptions(mode, self.output_var.get().strip(),
                                  self.filename_var.get().strip() if mode == "merge" else "合并结果.pdf",
                                  self.pages_var.get() if mode in ("extract", "rotate") else "",
                                  int(self.split_var.get()) if mode == "split" else 1,
                                  int(self.rotation_var.get()) if mode == "rotate" else 90, self.password_var.get())
        options.validate()
        return options

    def scan(self):
        if self.busy():
            return
        try:
            if not self.inputs:
                raise ValueError("请先添加 PDF 文件。")
            options = self._pdf_options()
        except (ValueError, core.FileToolError) as exc:
            messagebox.showerror("无法预览", str(exc), parent=self)
            return
        self.rows, self.options = [], options
        self._render()
        inputs, recursive = list(self.inputs), self.recursive_var.get()
        def work(report):
            # collect_files 会全局排序；逐个展开才能保留用户调整过的文件顺序。
            paths, seen = [], set()
            for value in inputs:
                for path in self._paths([value], recursive, "pdf"):
                    if core.key(path) not in seen:
                        paths.append(path)
                        seen.add(core.key(path))
            if not paths:
                raise core.FileToolError("所选范围没有 PDF 文件。")
            return core.build_pdf_plan(paths, options, self._stop, report)
        def finish(rows):
            self.rows = rows
            self._render()
            self.status_var.set(f"预览完成：{sum(not row.error for row in rows)} 项可执行，{sum(bool(row.error) for row in rows)} 项有问题。修改设置后需重新预览。")
        self._run(work, finish)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            paths = " → ".join(source.path.name for source in row.sources)
            output = str(row.destinations[0]) + (f" 等 {len(row.destinations)} 份" if len(row.destinations) > 1 else "") if row.destinations else "—"
            self.tree.insert("", "end", iid=str(index), values=(paths, sum(source.pages for source in row.sources), output, row.status, row.error or row.detail))

    def _detail(self, event=None):
        selected = self.tree.selection()
        if selected:
            row = self.rows[int(selected[0])]
            paths = "；".join(str(path) for path in row.destinations[:8])
            self.detail_var.set((row.error or row.detail) + "\n" + paths + ("；其余输出执行后可在任务记录查看" if len(row.destinations) > 8 else ""))

    def execute(self):
        if self.busy():
            return
        try:
            options = self._pdf_options()
        except (ValueError, core.FileToolError) as exc:
            messagebox.showerror("设置有误", str(exc), parent=self)
            return
        if options != self.options:
            messagebox.showinfo("请先预览", "设置已变化，请重新扫描预览。", parent=self)
            return
        rows = [row for row in self.rows if row.status == "待处理" and not row.error]
        if not rows:
            messagebox.showinfo("暂无任务", "请先扫描，检查输出路径和页码范围。", parent=self)
            return
        count = sum(len(row.destinations) for row in rows)
        if not messagebox.askyesno("确认 PDF 操作", f"将执行 {len(rows)} 项任务，生成 {count} 个 PDF。\n源文件保留，已有结果不覆盖。\n\n是否开始？", parent=self):
            return
        def finish(result):
            self._render()
            self.status_var.set(f"处理结束：成功 {sum(row.status == '已完成' for row in rows)} 项，失败 {sum(row.status == '失败' for row in rows)} 项。详细结果已保存到任务记录。")
        self._run(lambda report: core.execute_pdf_plan(rows, options, self._stop, report), finish,
                  task=("PDF · " + self.mode_var.get(), [source.path for row in rows for source in row.sources], lambda result: core.history_rows(rows)))

    def open_output(self):
        path = next((output.parent for row in self.rows for output in row.outputs), None)
        if path is None or not path.is_dir():
            messagebox.showinfo("暂无输出", "执行完成后可打开输出目录。", parent=self)
        elif os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
