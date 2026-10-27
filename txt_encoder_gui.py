#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
txt_encoder_gui.py —— 批量文本编码转换工具（图形界面版）

零依赖（只用 Python 标准库 tkinter），双击 run_gui.bat 即可运行。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txt_encoder_core import (  # noqa: E402
    BOM_MODES,
    DEFAULT_EXTS,
    ENCODING_CHOICES,
    NEWLINE_MODES,
    OUTPUT_MODES,
    TARGET_CHOICES,
    VERSION,
    ConvertOptions,
    EncodingNotSupported,
    convert_file,
    encoding_label,
    format_size,
    iter_files,
    parse_exts,
    target_tag,
)

ERR_MODES = {"跳过该文件（推荐）": "skip", "用替代字符继续": "replace"}
PREVIEW_BYTES = 262144
PREVIEW_CHARS = 20000


def read_preview(path: Path, encoding: str = "auto") -> tuple[str, str, str]:
    """读取文件开头用于预览。返回 (文本, 实际编码名, 提示)。"""
    from txt_encoder_core import decode_bytes, detect_file, looks_binary

    with path.open("rb") as fh:
        data = fh.read(PREVIEW_BYTES)
    truncated = path.stat().st_size > PREVIEW_BYTES
    if looks_binary(data):
        return "[疑似二进制文件，已跳过预览]", "", ""
    # 只看开头会漏掉“英文表头 + 中文正文”的大文件，这里用和命令行同一套取样逻辑
    detected = detect_file(path)
    codec = encoding
    if not encoding or str(encoding).lower() == "auto":
        codec = detected.encoding
    try:
        text, codec_used, _ = decode_bytes(data, codec, errors="replace")
    except (EncodingNotSupported, UnicodeError) as exc:
        return f"[无法用 {encoding_label(codec)} 解码：{exc}]", codec, ""
    note = ""
    if truncated:
        text = text[:PREVIEW_CHARS]
        note = f"（仅显示文件开头 {format_size(PREVIEW_BYTES)}）"
    else:
        text = text[:PREVIEW_CHARS]
    if encoding and str(encoding).lower() != "auto":
        note = (f"手动指定：{encoding_label(codec)}；自动检测结果是 "
                f"{detected.label}（{detected.confidence * 100:.0f}%）{note}")
    else:
        note = f"自动检测：{detected.label}（{detected.confidence * 100:.0f}%）· {detected.reason} {note}"
    return text, codec_used, note


class _EncoderUI:
    """转码界面，供独立窗口和工具箱页签共用。"""

    POLL_MS = 80

    def _init_encoder(self) -> None:
        self._encoder_closed = False
        self._pairs: list[tuple[Path, Path]] = []
        self._item_ids: list[str] = []
        self._results: dict[str, object] = {}
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

        self._init_style()
        self._build_vars()
        self._build_ui()
        self._poll_after = self.after(self.POLL_MS, self._poll_queue)
        self._convert_after = None

    def shutdown(self) -> None:
        if self._encoder_closed:
            return
        self._encoder_closed = True
        self._stop_event.set()
        self._pending_convert = None
        for timer in (self._poll_after, self._convert_after):
            if timer is not None:
                self.after_cancel(timer)

    # 样式
    def _init_style(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                f = tkfont.nametofont(name)
                if f.actual("family") not in ("Microsoft YaHei UI",):
                    f.configure(family="Microsoft YaHei UI", size=10)
            except tk.TclError:
                pass
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))

    def _build_vars(self) -> None:
        self.input_var = tk.StringVar(self)
        self.recursive_var = tk.BooleanVar(self, value=True)
        self.hidden_var = tk.BooleanVar(self, value=True)
        self.ext_var = tk.StringVar(self, value="txt,md,log,csv,json,xml,html,ini,yml,srt")
        self.src_labels = [label for label, _ in ENCODING_CHOICES]
        self.src_var = tk.StringVar(self, value=self.src_labels[0])
        self.tgt_labels = [label for label, _ in TARGET_CHOICES]
        self.tgt_var = tk.StringVar(self, value=self.tgt_labels[0])
        self.bom_var = tk.StringVar(self, value="与原文件一致")
        self.nl_var = tk.StringVar(self, value="保持原样")
        self.mode_var = tk.StringVar(self, value="newdir")
        self.out_dir_var = tk.StringVar(self)
        self.suffix_var = tk.StringVar(self, value="_utf8")
        self.backup_var = tk.BooleanVar(self, value=True)
        self.skip_same_var = tk.BooleanVar(self, value=True)
        self.err_var = tk.StringVar(self, value=list(ERR_MODES)[0])
        self.status_var = tk.StringVar(self, value="就绪。先设置好参数，然后点“扫描预览”。")
        self.preview_info_var = tk.StringVar(self, value="在左侧列表中选择一个文件即可预览内容。")
        self.preview_enc_var = tk.StringVar(self, value="按上面设置的源编码")

    # 界面
    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        self._build_input_box(outer)
        self._build_option_box(outer)
        self._build_action_box(outer)
        self._build_result_box(outer)

    def _build_input_box(self, parent) -> None:
        box = ttk.LabelFrame(parent, text=" 1. 选择要处理的文件 ", padding=10)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="文件或文件夹：").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(box, textvariable=self.input_var)
        entry.grid(row=0, column=1, sticky="we", padx=(0, 6))

        btns = ttk.Frame(box)
        btns.grid(row=0, column=2, sticky="e")
        ttk.Button(btns, text="选择文件夹…", command=self._pick_folder, width=12).pack(side="left", padx=2)
        ttk.Button(btns, text="选择文件…", command=self._pick_files, width=11).pack(side="left", padx=2)
        ttk.Button(btns, text="清空", command=self._clear_input, width=6).pack(side="left", padx=2)

        row2 = ttk.Frame(box)
        row2.grid(row=1, column=0, columnspan=3, sticky="we", pady=(8, 0))
        ttk.Label(row2, text="文件类型：").pack(side="left")
        ttk.Entry(row2, textvariable=self.ext_var, width=46).pack(side="left")
        ttk.Label(row2, text="（逗号分隔，不含点也可以；填 all 表示全部文件）",
                  foreground="#666").pack(side="left", padx=(6, 0))

        row3 = ttk.Frame(box)
        row3.grid(row=2, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Checkbutton(row3, text="包含子文件夹", variable=self.recursive_var).pack(side="left")
        ttk.Checkbutton(row3, text="跳过隐藏文件", variable=self.hidden_var).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(row3, text="已是目标编码的文件不重复转换",
                        variable=self.skip_same_var).pack(side="left", padx=(16, 0))

    def _build_option_box(self, parent) -> None:
        box = ttk.LabelFrame(parent, text=" 2. 转换设置 ", padding=10)
        box.pack(fill="x", pady=(10, 0))
        for col in (1, 3):
            box.columnconfigure(col, weight=1)

        ttk.Label(box, text="源编码：").grid(row=0, column=0, sticky="w", pady=3)
        self.src_combo = ttk.Combobox(box, textvariable=self.src_var, values=self.src_labels,
                                      state="readonly", width=26)
        self.src_combo.grid(row=0, column=1, sticky="w", pady=3, padx=(0, 16))
        self.src_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_preview())

        ttk.Label(box, text="目标编码：").grid(row=0, column=2, sticky="w", pady=3)
        self.tgt_combo = ttk.Combobox(box, textvariable=self.tgt_var, values=self.tgt_labels,
                                      state="readonly", width=34)
        self.tgt_combo.grid(row=0, column=3, sticky="w", pady=3)
        self.tgt_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_target_change())

        ttk.Label(box, text="BOM 处理：").grid(row=1, column=0, sticky="w", pady=3)
        bom_box = ttk.Combobox(box, textvariable=self.bom_var,
                               values=list(BOM_MODES.values()), state="readonly", width=26)
        bom_box.grid(row=1, column=1, sticky="w", pady=3, padx=(0, 16))

        ttk.Label(box, text="换行符：").grid(row=1, column=2, sticky="w", pady=3)
        nl_box = ttk.Combobox(box, textvariable=self.nl_var,
                              values=list(NEWLINE_MODES.values()), state="readonly", width=34)
        nl_box.grid(row=1, column=3, sticky="w", pady=3)

        ttk.Label(box, text="解码失败时：").grid(row=2, column=0, sticky="w", pady=3)
        err_box = ttk.Combobox(box, textvariable=self.err_var,
                               values=list(ERR_MODES), state="readonly", width=26)
        err_box.grid(row=2, column=1, sticky="w", pady=3, padx=(0, 16))

        mode_row = ttk.Frame(box)
        mode_row.grid(row=2, column=2, columnspan=2, sticky="w", pady=3)
        ttk.Label(mode_row, text="输出方式：").pack(side="left")
        for key, text in OUTPUT_MODES.items():
            ttk.Radiobutton(mode_row, text=text, value=key, variable=self.mode_var,
                            command=self._on_mode_change).pack(side="left", padx=(4, 10))

        path_row = ttk.Frame(box)
        path_row.grid(row=3, column=0, columnspan=4, sticky="we", pady=(6, 0))
        path_row.columnconfigure(1, weight=1)
        ttk.Label(path_row, text="输出目录：").grid(row=0, column=0, sticky="w")
        ttk.Entry(path_row, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="we", padx=(0, 6))
        ttk.Button(path_row, text="选择…", command=self._pick_out_dir, width=8).grid(row=0, column=2)
        ttk.Label(path_row, text="  后缀：").grid(row=0, column=3, sticky="w")
        ttk.Entry(path_row, textvariable=self.suffix_var, width=14).grid(row=0, column=4)
        self.backup_check = ttk.Checkbutton(path_row, text="原地覆盖时备份 .bak", variable=self.backup_var)
        self.backup_check.grid(row=0, column=5, padx=(12, 0))

        self.hint_label = ttk.Label(
            box,
            text="提示：输出到新目录时，留空“输出目录”会在源文件夹旁生成 “文件夹名_utf8” 目录，且不会动原文件。",
            foreground="#666",
        )
        self.hint_label.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._on_mode_change()

    def _build_action_box(self, parent) -> None:
        box = ttk.Frame(parent)
        box.pack(fill="x", pady=(10, 0))

        self.scan_btn = ttk.Button(box, text="① 扫描预览", command=self._start_scan, width=14)
        self.scan_btn.pack(side="left")
        self.run_btn = ttk.Button(box, text="② 开始转换", command=self._start_convert, width=14)
        self.run_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(box, text="停止", command=self._stop, width=8, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Button(box, text="打开输出目录", command=self._open_output, width=12).pack(side="left", padx=6)
        ttk.Button(box, text="保存报告…", command=self._save_report, width=11).pack(side="left")

        self.progress = ttk.Progressbar(box, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 8))
        ttk.Label(box, textvariable=self.status_var, width=42).pack(side="left")

    def _build_result_box(self, parent) -> None:
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True, pady=(10, 0))

        self.notebook = ttk.Notebook(box)
        self.notebook.pack(fill="both", expand=True)

        # 文件列表
        list_tab = ttk.Frame(self.notebook)
        self.notebook.add(list_tab, text="  文件列表  ")
        columns = ("status", "name", "src", "conf", "size", "note")
        self.tree = ttk.Treeview(list_tab, columns=columns, show="headings", height=14)
        heads = {"status": ("状态", 110), "name": ("文件路径", 420), "src": ("检测到的源编码", 190),
                 "conf": ("置信度", 70), "size": ("大小", 90), "note": ("说明", 380)}
        for key, (text, width) in heads.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w",
                             stretch=(key in ("name", "note")))
        self.tree.tag_configure("converted", foreground="#0a7a2f")
        self.tree.tag_configure("failed", foreground="#c00000")
        self.tree.tag_configure("unchanged", foreground="#888888")
        self.tree.tag_configure("copied", foreground="#a06000")
        self.tree.tag_configure("skipped", foreground="#888888")

        vsb = ttk.Scrollbar(list_tab, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(list_tab, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="we")
        list_tab.rowconfigure(0, weight=1)
        list_tab.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_preview())

        # 内容预览
        prev_tab = ttk.Frame(self.notebook)
        self.notebook.add(prev_tab, text="  内容预览 / 校验  ")
        top = ttk.Frame(prev_tab, padding=(6, 6))
        top.pack(fill="x")
        ttk.Label(top, text="用这个编码来解码预览：").pack(side="left")
        preview_labels = ["按上面设置的源编码"] + self.src_labels
        self.preview_combo = ttk.Combobox(top, textvariable=self.preview_enc_var,
                                          values=preview_labels, state="readonly", width=26)
        self.preview_combo.pack(side="left", padx=6)
        self.preview_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_preview())
        ttk.Label(top, textvariable=self.preview_info_var, foreground="#444").pack(side="left", padx=10)

        text_wrap = ttk.Frame(prev_tab)
        text_wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.preview_text = tk.Text(text_wrap, wrap="none", font=("Consolas", 10), undo=False)
        pvs = ttk.Scrollbar(text_wrap, orient="vertical", command=self.preview_text.yview)
        phs = ttk.Scrollbar(text_wrap, orient="horizontal", command=self.preview_text.xview)
        self.preview_text.configure(yscrollcommand=pvs.set, xscrollcommand=phs.set)
        self.preview_text.grid(row=0, column=0, sticky="nsew")
        pvs.grid(row=0, column=1, sticky="ns")
        phs.grid(row=1, column=0, sticky="we")
        text_wrap.rowconfigure(0, weight=1)
        text_wrap.columnconfigure(0, weight=1)

        # 日志
        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="  日志  ")
        self.log_text = tk.Text(log_tab, wrap="word", height=8, font=("Consolas", 9))
        lsb = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    # 交互
    def _pick_folder(self) -> None:
        path = filedialog.askdirectory(title="选择包含待转换文本的文件夹")
        if path:
            self._add_input(path)

    def _pick_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择要转换的文件")
        if paths:
            for item in paths:
                self._add_input(item)

    def _add_input(self, path: str) -> None:
        current = self.input_var.get().strip()
        items = [p for p in current.split(";") if p.strip()] if current else []
        if path not in items:
            items.append(path)
        self.input_var.set(";".join(items))

    def _clear_input(self) -> None:
        self.input_var.set("")
        self.tree.delete(*self.tree.get_children())
        self._pairs.clear()
        self._item_ids.clear()
        self._results.clear()
        self.progress["value"] = 0
        self.status_var.set("已清空。")

    def _pick_out_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_dir_var.set(path)
            self.mode_var.set("newdir")
            self._on_mode_change()

    def _on_target_change(self) -> None:
        codec = self._selected_codec(self.tgt_var.get(), TARGET_CHOICES)
        self.suffix_var.set("_" + target_tag(codec, "none", False))
        self._refresh_preview()

    def _on_mode_change(self) -> None:
        mode = self.mode_var.get()
        self.hint_label.configure(text={
            "newdir": "输出到新目录：原文件不动，结果写进新目录；留空则自动生成 “文件夹名_utf8”。",
            "suffix": f"原目录另存：在原文件旁生成 “文件名{self.suffix_var.get()}.txt”，原文件保留。",
            "inplace": "原地覆盖：直接改写原文件，建议保持备份勾选；转换失败的文件不会被改动。",
        }.get(mode, ""))

    def _selected_codec(self, label: str, choices) -> str:
        for text, codec in choices:
            if text == label:
                return codec
        return label

    def _current_source(self) -> str:
        return self._selected_codec(self.src_var.get(), ENCODING_CHOICES)

    def _current_target(self) -> str:
        return self._selected_codec(self.tgt_var.get(), TARGET_CHOICES)

    def _bom_mode(self) -> str:
        for key, text in BOM_MODES.items():
            if text == self.bom_var.get():
                return key
        return "keep"

    def _newline_mode(self) -> str:
        for key, text in NEWLINE_MODES.items():
            if text == self.nl_var.get():
                return key
        return "keep"

    # 参数
    def _make_options(self, dry_run: bool) -> ConvertOptions:
        out_dir = self.out_dir_var.get().strip()
        return ConvertOptions(
            source_encoding=self._current_source(),
            target_encoding=self._current_target(),
            bom_mode=self._bom_mode(),
            newline=self._newline_mode(),
            on_decode_error=ERR_MODES.get(self.err_var.get(), "skip"),
            on_encode_error=ERR_MODES.get(self.err_var.get(), "skip"),
            skip_if_same=self.skip_same_var.get(),
            copy_unchanged=True,
            output_mode=self.mode_var.get(),
            output_dir=Path(out_dir) if out_dir else None,
            suffix=self.suffix_var.get() or "_converted",
            backup=self.backup_var.get(),
            dry_run=dry_run,
        )

    def _collect_inputs(self) -> list[Path]:
        raw = self.input_var.get().strip()
        if not raw:
            return []
        items = [Path(p.strip()) for p in raw.replace("\n", ";").split(";") if p.strip()]
        return [p for p in items if p.exists()]

    def _exclude_dirs(self, options: ConvertOptions) -> list[Path]:
        if options.output_dir is not None:
            return [options.output_dir]
        return []

    # 任务
    def _start_scan(self) -> None:
        options = self._make_options(dry_run=True)
        inputs = self._collect_inputs()
        if not self._prepare(inputs, "扫描" ):
            return
        self._run_worker(options, scan=True)

    def _start_convert(self) -> None:
        options = self._make_options(dry_run=False)
        inputs = self._collect_inputs()
        if not inputs:
            messagebox.showwarning("请先选择文件", "请先选择要处理的文件夹或文件。")
            return

        if options.output_mode == "inplace" and not options.backup:
            if not messagebox.askyesno(
                "确认原地覆盖",
                "你选择了“原地覆盖原文件”且没有备份。\n\n"
                "原文件内容将被永久替换，无法还原。\n确定继续吗？",
                icon="warning",
            ):
                return

        if not self._pairs:
            if not self._prepare(inputs, "转换"):
                return
            self._run_worker(options, scan=True)
            self.status_var.set("正在扫描，扫描完成后会自动开始转换…")
            self._pending_convert = options
            return
        self._run_worker(options, scan=False)

    def _prepare(self, inputs: list[Path], action: str) -> bool:
        if not inputs:
            messagebox.showwarning("请先选择文件", "请先选择要处理的文件夹或文件。")
            return False
        options = self._make_options(dry_run=True)
        try:
            pairs = list(iter_files(
                inputs,
                parse_exts(self.ext_var.get()),
                self.recursive_var.get(),
                self._exclude_dirs(options),
                skip_hidden=self.hidden_var.get(),
            ))
        except OSError as exc:
            messagebox.showerror("无法读取目录", str(exc))
            return False
        if not pairs:
            messagebox.showinfo(
                "没有找到文件",
                "没有找到符合条件（扩展名 / 排除项）的文件。\n"
                "可以试试把“文件类型”改成 all。",
            )
            return False
        self._pairs = pairs
        self._item_ids = []
        self._results.clear()
        self.tree.delete(*self.tree.get_children())
        self._log(f"共找到 {len(pairs)} 个文件，准备{action}。")
        return True

    def _run_worker(self, options: ConvertOptions, scan: bool) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._set_busy(True)
        self.progress.configure(maximum=len(self._pairs), value=0)
        self._worker = threading.Thread(
            target=self._worker_main, args=(options, scan), daemon=True,
        )
        self._worker.start()

    def _worker_main(self, options: ConvertOptions, scan: bool) -> None:
        total = len(self._pairs)
        from task_history import file_rows, item
        history = getattr(self, "history", None)
        token = history.begin("TXT 编码检测" if scan else "TXT 批量转码", [src for src, root in self._pairs]) if history else None
        recorded, failures = [], []
        try:
            for index, (src, root) in enumerate(self._pairs):
                if self._stop_event.is_set():
                    self._queue.put(("stopped", index, total))
                    return
                try:
                    result = convert_file(src, options, root)
                except Exception as exc:  # 单个文件出错不影响整体
                    self._queue.put(("crash", index, f"{src}: {exc}"))
                    failures.append(item(src, state="失败", detail=str(exc)))
                    continue
                recorded.append(result)
                self._queue.put(("result", index, result))
                self._queue.put(("progress", index + 1, total))
        finally:
            if history:
                history.finish(token, items=file_rows(recorded) + failures, cancelled=self._stop_event.is_set())
            self._queue.put(("done", scan, total))

    # 反馈
    def _poll_queue(self) -> None:
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "result":
                    _, index, result = message
                    self._apply_result(index, result)
                elif kind == "progress":
                    _, done, total = message
                    self.progress["value"] = done
                    self.status_var.set(f"处理中… {done}/{total}")
                elif kind == "crash":
                    _, _index, text = message
                    self._log(f"[异常] {text}")
                elif kind == "stopped":
                    _, index, total = message
                    self._log(f"已被用户停止，处理到第 {index} / {total} 个文件。")
                    self._set_busy(False)
                elif kind == "done":
                    _, scan, total = message
                    self._finish(scan, total)
        except queue.Empty:
            pass
        self._poll_after = self.after(self.POLL_MS, self._poll_queue)

    def _apply_result(self, index: int, result) -> None:
        self._results[str(result.src)] = result
        note = result.error or result.reason
        if result.status == "copied":
            note = "已是目标编码，直接复制到输出目录"
        elif result.status == "unchanged":
            note = "已是目标编码，无需转换"
        values = (
            result.status_text(),
            str(result.src),
            result.source_label or "—",
            f"{result.confidence * 100:.0f}%" if result.confidence else "—",
            format_size(result.size_before),
            note,
        )
        tag = result.status
        if index < len(self._item_ids):
            iid = self._item_ids[index]
            self.tree.item(iid, values=values, tags=(tag,))
        else:
            iid = self.tree.insert("", "end", values=values, tags=(tag,))
            while len(self._item_ids) < index:
                self._item_ids.append("")
            if len(self._item_ids) == index:
                self._item_ids.append(iid)
            else:
                self._item_ids[index] = iid
        if result.status == "failed":
            self._log(f"[失败] {result.src} —— {result.error}")

    def _finish(self, scan: bool, total: int) -> None:
        self._set_busy(False)
        results = list(self._results.values())
        converted = sum(1 for r in results if r.status == "converted")
        failed = [r for r in results if r.status == "failed"]
        unchanged = sum(1 for r in results if r.status in ("unchanged", "copied"))
        skipped = sum(1 for r in results if r.status == "skipped")
        preview = sum(1 for r in results if r.status == "preview")

        if scan:
            self.status_var.set(f"扫描完成：{preview} 个需要转换，{unchanged} 个已符合，{failed and len(failed) or 0} 个有问题")
            self._log(
                f"扫描完成：共 {total} 个文件，其中 {preview} 个需要转换，"
                f"{unchanged} 个已是目标编码，{skipped} 个疑似二进制已跳过，{len(failed)} 个失败。"
            )
            if preview and self._pending_convert_options():
                self._pending_convert = None
                self._convert_after = self.after(50, self._continue_convert)
            self.notebook.select(0)
            return

        self.status_var.set(f"完成：成功 {converted}，跳过 {unchanged}，失败 {len(failed)}")
        self._log(
            f"转换完成：共 {total} 个文件，成功 {converted}，已符合 {unchanged}，"
            f"跳过 {skipped}，失败 {len(failed)}。"
        )
        out = self._make_options(False).output_dir
        if converted and out is not None:
            self._log(f"输出目录：{out}")
        self.notebook.select(0)

    def _pending_convert_options(self):
        return getattr(self, "_pending_convert", None)

    def _continue_convert(self) -> None:
        options = self._pending_convert_options()
        if options is None:
            return
        self._pending_convert = None
        if not self._pairs:
            return
        self._log("扫描结束，开始真正转换…")
        self._run_worker(options, scan=False)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_btn.configure(state=state)
        self.run_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _stop(self) -> None:
        self._stop_event.set()
        self._log("正在停止…（当前文件处理完就停）")

    def _log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    # 预览
    def _refresh_preview(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = self.tree.index(selection[0])
        if index >= len(self._pairs):
            return
        path = self._pairs[index][0]
        selected = self.preview_enc_var.get()
        encoding = self._current_source()
        if selected and selected != "按上面设置的源编码":
            encoding = self._selected_codec(selected, ENCODING_CHOICES)
        try:
            text, _codec, note = read_preview(path, encoding)
        except OSError as exc:
            text, note = f"[读取失败：{exc}]", ""
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")
        self.preview_info_var.set(f"{path.name} · {note}")

    # 输出
    def _open_output(self) -> None:
        options = self._make_options(False)
        target = options.output_dir
        if target is None and self._pairs:
            from txt_encoder_core import build_destination
            try:
                target = build_destination(self._pairs[0][0], self._pairs[0][1], options).parent
            except Exception:
                target = None
        if target is None or not Path(target).exists():
            messagebox.showinfo("暂无输出目录", "还没有可打开的输出目录。")
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def _save_report(self) -> None:
        if not self._results:
            messagebox.showinfo("暂无数据", "请先扫描或转换，然后再保存报告。")
            return
        path = filedialog.asksaveasfilename(
            title="保存转换报告", defaultextension=".csv",
            initialfile="编码转换报告.csv",
            filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["源文件", "输出文件", "状态", "源编码", "置信度",
                             "目标编码", "原大小", "新大小", "说明", "错误"])
            for item in self._results.values():
                writer.writerow([
                    str(item.src), str(item.dst or ""), item.status_text(),
                    item.source_label or item.source_encoding, f"{item.confidence:.2f}",
                    encoding_label(item.target_encoding), item.size_before, item.size_after,
                    item.reason, item.error,
                ])
        messagebox.showinfo("已保存", f"报告已保存到：\n{path}")


class EncoderPanel(_EncoderUI, ttk.Frame):
    """可嵌入其他窗口的转码页。"""

    def __init__(self, master) -> None:
        super().__init__(master)
        self._init_encoder()

    def destroy(self) -> None:
        self.shutdown()
        super().destroy()


class EncoderApp(_EncoderUI, tk.Tk):
    """兼容原来的独立 GUI 入口。"""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"批量文本编码转换工具  v{VERSION}")
        self.geometry("1080x760")
        self.minsize(920, 660)
        self._init_encoder()

    def destroy(self) -> None:
        self.shutdown()
        super().destroy()


def main() -> int:
    app = EncoderApp()
    # 支持把文件夹拖到 run_gui.bat 上直接打开
    for arg in sys.argv[1:]:
        if Path(arg).exists():
            app._add_input(str(Path(arg)))
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # 图形界面下也要能看到错误
        detail = traceback.format_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("程序出错", detail)
            root.destroy()
        except Exception:
            print(detail, file=sys.stderr)
        raise SystemExit(1)
