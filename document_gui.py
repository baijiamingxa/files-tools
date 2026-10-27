#!/usr/bin/env python3
"""Word 与 PDF 转换页。"""
from __future__ import annotations

import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from file_tools_gui import FilePanel
import document_conversion as core
from document_runtime import ensure_office, find_office

MODES = {"自动识别 Word ↔ PDF": "auto", "Word → PDF": "word_pdf", "PDF → Word（DOCX）": "pdf_word"}


class DocumentPanel(FilePanel):
    def __init__(self, parent, status_callback=None):
        super().__init__(parent, status_callback, extensions="doc,docx,pdf")
        self.checked = set()
        self.options = None
        self.mode_var = tk.StringVar(self, value=next(iter(MODES)))
        self.output_var = tk.StringVar(self)
        self.password_var = tk.StringVar(self)
        self.timeout_var = tk.StringVar(self, value="20")
        options = ttk.LabelFrame(self.controls, text="转换设置", padding=8)
        options.pack(fill="x", pady=8)
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="转换方向：").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, textvariable=self.mode_var, values=list(MODES), state="readonly", width=28).grid(row=0, column=1, sticky="w")
        ttk.Label(options, text="单文件超时（分钟）：").grid(row=0, column=2, padx=(12, 4))
        ttk.Entry(options, textvariable=self.timeout_var, width=6).grid(row=0, column=3)
        ttk.Label(options, text="输出目录：").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(options, textvariable=self.output_var).grid(row=1, column=1, columnspan=3, sticky="ew", pady=6)
        ttk.Button(options, text="浏览…", command=self._output_folder).grid(row=1, column=4, padx=(8, 0))
        ttk.Label(options, text="PDF 密码（若有）：").grid(row=2, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.password_var, show="*", width=28).grid(row=2, column=1, sticky="w")
        ttk.Label(options, text="留空输出到原目录的“文档转换结果”；不覆盖源文件和已有结果。").grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(options, text="普通 PDF 转可编辑 DOCX；扫描件保留页面图片，不做 OCR。复杂排版可能需要调整。").grid(row=4, column=0, columnspan=5, sticky="w")
        self.actions.pack(fill="x")
        self.scan_btn = ttk.Button(self.actions, text="1. 扫描预览", command=self.scan)
        self.scan_btn.pack(side="left")
        self.convert_btn = ttk.Button(self.actions, text="2. 转换勾选项…", command=self.convert)
        self.convert_btn.pack(side="left", padx=6)
        ttk.Button(self.actions, text="配置 Word 转 PDF 引擎", command=self.setup_engine).pack(side="left")
        ttk.Button(self.actions, text="打开输出目录", command=self.open_output).pack(side="left", padx=6)
        self._table((("check", "勾选", 60), ("path", "源文件", 270), ("destination", "输出文件", 270), ("mode", "方向", 100), ("state", "状态", 90), ("detail", "结果 / 错误", 320)))
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", self._toggle_selection)
        self.tree.bind("<<TreeviewSelect>>", self._detail)
        self.detail_var = tk.StringVar(self, value="选择结果行可查看完整路径与转换说明。")
        ttk.Label(self, textvariable=self.detail_var, wraplength=1050).pack(fill="x", before=self.results, pady=(6, 0))
        for variable in (self.mode_var, self.output_var, self.password_var, self.timeout_var):
            variable.trace_add("write", lambda *args: self.invalidate())

    def _output_folder(self):
        path = filedialog.askdirectory(parent=self)
        if path:
            self.output_var.set(path)

    def _document_options(self):
        options = core.DocumentOptions(MODES[self.mode_var.get()], self.output_var.get().strip(), self.password_var.get(), int(float(self.timeout_var.get()) * 60))
        options.validate()
        return options

    def scan(self):
        if self.busy():
            return
        try:
            options = self._document_options()
            if not self.inputs:
                raise ValueError("请先选择 Word 或 PDF 文件。")
        except (ValueError, core.FileToolError) as exc:
            messagebox.showerror("无法扫描", str(exc), parent=self)
            return
        self.rows, self.checked, self.options = [], set(), options
        self._render()
        inputs, recursive, extensions = list(self.inputs), self.recursive_var.get(), self.extensions_var.get()
        def work(report):
            return core.build_document_plan(self._paths(inputs, recursive, extensions), options, self._stop, report)
        def finish(rows):
            self.rows = rows
            self.checked = {index for index, row in enumerate(rows) if row.status == "待转换" and not row.error}
            self._render()
            self.status_var.set(f"预览完成：{len(rows)} 个文件，{len(self.checked)} 个可转换；核对方向和输出路径后执行。")
        self._run(work, finish)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(index), values=("☑" if index in self.checked else "☐", str(row.path), str(row.destination), "PDF → Word" if row.mode == "pdf_word" else "Word → PDF", row.status, row.error or row.detail))

    def _click(self, event):
        item = self.tree.identify_row(event.y)
        if item and self.tree.identify_column(event.x) == "#1":
            self._toggle(int(item))
            return "break"

    def _toggle(self, index):
        if self.busy() or self.rows[index].status != "待转换" or self.rows[index].error:
            return
        if index in self.checked:
            self.checked.remove(index)
        else:
            self.checked.add(index)
        self.tree.set(str(index), "check", "☑" if index in self.checked else "☐")

    def _toggle_selection(self, event):
        for item in self.tree.selection():
            self._toggle(int(item))
        return "break"

    def _detail(self, event=None):
        selected = self.tree.selection()
        if selected:
            row = self.rows[int(selected[0])]
            self.detail_var.set(f"源文件：{row.path}\n输出：{row.destination}\n{row.error or row.detail}")

    def convert(self):
        if self.busy():
            return
        try:
            options = self._document_options()
        except (ValueError, core.FileToolError) as exc:
            messagebox.showerror("设置有误", str(exc), parent=self)
            return
        if options != self.options:
            messagebox.showinfo("请重新预览", "转换设置已变化，请先扫描预览。", parent=self)
            return
        selected = [self.rows[index] for index in sorted(self.checked) if index < len(self.rows) and self.rows[index].status == "待转换" and not self.rows[index].error]
        if not selected:
            messagebox.showinfo("暂无任务", "请先扫描并勾选待转换文件。", parent=self)
            return
        listing = "\n".join(str(row.destination) for row in selected[:15])
        if not messagebox.askyesno("确认文档转换", f"将转换 {len(selected)} 个文档，源文件保留。\n\n{listing}\n\n若缺少 Word 引擎，将自动下载并配置官方 LibreOffice。确认开始？", parent=self):
            return
        def finish(rows):
            self.checked.clear()
            self._render()
            self.status_var.set(f"转换完成：成功 {sum(row.status == '已完成' for row in selected)}，失败 {sum(row.status == '失败' for row in selected)}；源文件保留。")
        self._run(lambda report: core.execute_document_plan(selected, options, self._stop, report), finish)

    def setup_engine(self):
        def finish(path):
            self.status_var.set(f"Word 转 PDF 引擎可用：{path}")
        def work(report):
            try:
                return ensure_office(self._stop, lambda text: report(0, 1, text))
            except core.runtime.RuntimeCancelled:
                raise core.FileToolCancelled("已取消文档引擎配置。") from None
        self._run(work, finish)

    def open_output(self):
        path = Path(self.output_var.get()).expanduser() if self.output_var.get().strip() else (self.rows[0].destination.parent if self.rows else None)
        if path is None or not path.is_dir():
            messagebox.showinfo("暂无输出目录", "转换完成后可打开输出目录。", parent=self)
            return
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
