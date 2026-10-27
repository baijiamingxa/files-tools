#!/usr/bin/env python3
"""任务记录窗口；清除记录只操作数据库，不操作结果文件。"""
from __future__ import annotations

import os
import subprocess
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

from task_history import STATES


class HistoryWindow(tk.Toplevel):
    def __init__(self, parent, recorder):
        super().__init__(parent)
        self.recorder = recorder
        self.title("文件工具箱 · 任务记录")
        self.geometry("1080x650")
        self.minsize(850, 500)
        self.records = {}
        self._loaded_id = None
        self.filter_var = tk.StringVar(self)
        self.state_var = tk.StringVar(self, value="全部")
        bar = ttk.Frame(self, padding=10)
        bar.pack(fill="x")
        ttk.Label(bar, text="搜索任务 / 路径：").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.filter_var, width=30)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda event: self.refresh())
        ttk.Combobox(bar, textvariable=self.state_var, values=("全部", *STATES), width=10, state="readonly").pack(side="left", padx=8)
        self.state_var.trace_add("write", lambda *args: self.refresh())
        ttk.Button(bar, text="查询 / 刷新", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="导出 CSV…", command=self.export).pack(side="left", padx=6)
        ttk.Button(bar, text="清除记录…", command=self.clear).pack(side="left")
        ttk.Label(self, text="本机保留最近 2000 项任务、每项最多 5000 条明细，列表最多显示 500 项。密码、链接查询参数和文件正文不记录。", padding=(10, 0)).pack(anchor="w")
        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=10, pady=8)
        top, lower = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(top, weight=1)
        panes.add(lower, weight=1)
        self.tree = self._tree(top, (("time", "开始时间", 150), ("task", "任务", 210), ("state", "状态", 90), ("duration", "用时", 70), ("summary", "摘要", 440)))
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.detail = self._tree(lower, (("input", "源文件 / 链接", 330), ("output", "输出", 300), ("state", "状态", 85), ("detail", "说明 / 失败原因", 350)))
        self.detail.bind("<<TreeviewSelect>>", self.select_detail)
        self.note = tk.StringVar(self, value="选择任务可查看每个文件的结果；进行中任务可手动刷新。")
        note_frame = ttk.Frame(self, padding=(10, 4))
        note_frame.pack(side="bottom", fill="x", before=panes)
        note_text = tk.Text(note_frame, height=3, wrap="word", state="disabled")
        note_text.pack(side="left", fill="x", expand=True)
        note_scroll = ttk.Scrollbar(note_frame, orient="vertical", command=note_text.yview)
        note_scroll.pack(side="right", fill="y")
        note_text.configure(yscrollcommand=note_scroll.set)
        def update_note(*args):
            note_text.configure(state="normal")
            note_text.delete("1.0", "end")
            note_text.insert("1.0", self.note.get())
            note_text.configure(state="disabled")
        self.note.trace_add("write", update_note)
        update_note()
        actions = ttk.Frame(self, padding=(10, 4, 10, 10))
        actions.pack(side="bottom", fill="x", before=note_frame)
        ttk.Button(actions, text="打开所选输出的文件夹", command=self.open_output).pack(side="left")
        ttk.Button(actions, text="复制所选明细", command=self.copy_detail).pack(side="left", padx=8)
        ttk.Button(actions, text="关闭", command=self.destroy).pack(side="right")
        self.refresh()

    def _tree(self, parent, columns):
        tree = ttk.Treeview(parent, columns=[column[0] for column in columns], show="headings", selectmode="browse")
        for name, title, width in columns:
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=60)
        y = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        x = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        return tree

    def refresh(self):
        if not self.recorder.store:
            self.note.set("任务记录不可用：" + self.recorder.error)
            return
        try:
            selected = self.tree.selection()
            records = self.recorder.store.query(self.filter_var.get().strip(), self.state_var.get(), include_items=False)
            self.records = {record["id"]: record for record in records}
            self.tree.delete(*self.tree.get_children())
            self.detail.delete(*self.detail.get_children())
            self._loaded_id = None
            for record in records:
                duration = f"{max(0, (record['ended'] or time.time()) - record['started']):.1f} 秒"
                self.tree.insert("", "end", iid=record["id"], values=(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record["started"])), record["kind"], record["status"], duration, record["summary"].replace("\n", " · ")))
            if selected and selected[0] in self.records:
                self.tree.selection_set(selected[0])
                self.select()
            self.note.set(f"显示 {len(records)} 项任务。" + ("最近记录写入失败：" + self.recorder.error if self.recorder.error else "选择任务查看完整结果，清除记录不会删除文件。"))
        except Exception as exc:
            self.note.set("无法读取任务记录：" + str(exc))

    def selected_record(self):
        selected = self.tree.selection()
        return self.records.get(selected[0]) if selected else None

    def select(self, event=None):
        record = self.selected_record()
        # refresh / selection_set 会排入异步选择事件；同一任务不重复清空明细选择。
        if record and self._loaded_id == record["id"]:
            return
        if self._loaded_id in self.records:
            self.records[self._loaded_id]["items"] = []
        self.detail.delete(*self.detail.get_children())
        self._loaded_id = record["id"] if record else None
        if record:
            try:
                record = self.recorder.store.get(record["id"])
                if record is None:
                    self.note.set("该记录已被清除，请刷新列表。")
                    return
                self.records[record["id"]] = record
            except Exception as exc:
                self._loaded_id = None
                self.note.set("无法读取任务明细：" + str(exc))
                return
            for index, row in enumerate(record["items"]):
                self.detail.insert("", "end", iid=str(index), values=tuple(row[key].replace("\n", "；") for key in ("input", "output", "state", "detail")))
            self.note.set(record["summary"])

    def selected_item(self):
        record, selected = self.selected_record(), self.detail.selection()
        return record["items"][int(selected[0])] if record and selected else None

    def select_detail(self, event=None):
        row = self.selected_item()
        if row:
            self.note.set("\n".join(value for value in (row["input"], row["output"], row["detail"]) if value))

    def open_output(self):
        row = self.selected_item()
        if not row or not row["output"]:
            messagebox.showinfo("请选择输出", "请在下方明细选择有输出路径的结果。", parent=self)
            return
        path = Path(row["output"])
        if not path.exists():
            messagebox.showinfo("输出已移动", "该输出已移动或删除，记录不会自动重建文件。", parent=self)
            return
        folder = path if path.is_dir() else path.parent
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc), parent=self)

    def copy_detail(self):
        row = self.selected_item()
        if row:
            self.clipboard_clear()
            self.clipboard_append("\n".join(f"{name}：{row[key]}" for key, name in (("input", "源文件"), ("output", "输出"), ("state", "状态"), ("detail", "说明"))))

    def export(self, path=None):
        if not self.recorder.store:
            return
        path = path or filedialog.asksaveasfilename(parent=self, title="导出当前筛选的任务记录", defaultextension=".csv", initialfile="任务记录.csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                count = self.recorder.store.export(path, self.filter_var.get().strip(), self.state_var.get())
                self.note.set(f"已导出 {count} 项任务：{path}")
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc), parent=self)

    def clear(self):
        if not self.recorder.store:
            return
        selected = self.selected_record()
        text = "清除所选任务的记录？" if selected else "清除全部已结束的任务记录？"
        if messagebox.askyesno("清除任务记录", text + "\n只清除本机记录，不会删除源文件和输出；进行中记录会保留。", parent=self):
            try:
                self.recorder.store.delete([selected["id"]] if selected else None)
                self.refresh()
            except Exception as exc:
                messagebox.showerror("清除失败", str(exc), parent=self)
