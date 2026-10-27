#!/usr/bin/env python3
"""真实创建工具箱，走完下载→解压→预览→转码并检查原文件。"""

from __future__ import annotations

import io
import csv
import os
import tempfile
import time
import tkinter as tk
import tkinter.messagebox
import zipfile
from pathlib import Path
from unittest.mock import patch

import c_cleaner
import storage_tools
import swift_extract as se
from file_toolbox import ToolboxApp
from toolbox_selftest import local_server
from txt_encoder_core import make_console_tolerant


def pump(app, until, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if until():
            return True
        time.sleep(0.01)
    raise AssertionError("等待 GUI 任务超时")


def main():
    make_console_tolerant()
    text = "统一工具箱的图形界面测试。\r\n原文件必须保留。\r\n" * 100
    raw = text.encode("gbk")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as zf:
        zf.writestr("中文目录/文稿.txt", raw)
        zf.writestr("data.bin", bytes(range(256)) * 2048)
    with tempfile.TemporaryDirectory() as temp, local_server(archive_bytes.getvalue(), slow_delay=0.08) as base:
        root = Path(temp)
        app = ToolboxApp(config_path=root / "settings.json")
        errors = []
        def report_error(kind, value, trace):
            errors.append(f"{kind.__name__}: {value}")
        app.report_callback_exception = report_error
        try:
            app.update()
            assert len(app.tabs.tabs()) == 7
            assert sorted(image.width() for image in app._icon_images) == [32, 64]
            # 所有工具共用一个 Tk 解释器，没有额外主窗口。
            assert app.encoder.tk is app.download.root.tk is app.archive_tab.tk
            print("[通过] 七个工具嵌入同一个窗口")

            app.download.dir_var.set(str(root))
            app.download.name_var.set("下载结果.zip")
            app.download.url_var.set(base + "/range")
            app.download.threads_var.set(4)
            app.download.start_btn.invoke()
            app.tabs.select(app.encoder)
            assert app.download.url_var.get() == base + "/range"
            pump(app, lambda: app.last_download is not None)
            downloaded = root / "下载结果.zip"
            assert downloaded.read_bytes() == archive_bytes.getvalue()
            print("[通过] 切换页签后下载继续，文件逐字节一致")

            app.archive.out_dir_var.set(str(root / "解压"))
            app.extract_download_btn.invoke()
            assert app.tabs.select() == str(app.archive_tab)
            assert len(app.archive.jobs) == 1
            assert app.archive.jobs[0].state == "等待"
            app.archive.start_all()
            pump(app, lambda: not app.archive.busy())
            job = app.archive.jobs[0]
            assert job.state == "完成", job.error
            original = Path(job.dest) / "中文目录/文稿.txt"
            assert original.read_bytes() == raw
            print("[通过] 下载结果送入解压页，由按钮启动并完成解压")

            app.archive.send_output()
            assert app.tabs.select() == str(app.encoder)
            assert job.dest in app.encoder.input_var.get()
            assert app.encoder._worker is None
            app.encoder.out_dir_var.set(str(root / "utf8"))
            app.encoder._start_scan()
            pump(app, lambda: "扫描完成" in app.encoder.status_var.get())
            rows = app.encoder.tree.get_children()
            assert len(rows) == 1
            app.encoder.tree.selection_set(rows[0])
            app.encoder._refresh_preview()
            assert "统一工具箱" in app.encoder.preview_text.get("1.0", "end")
            app.encoder._start_convert()
            pump(app, lambda: app.encoder.status_var.get().startswith("完成："))
            out = root / "utf8/中文目录/文稿.txt"
            assert out.read_bytes().decode("utf-8") == text
            assert original.read_bytes() == raw
            print("[通过] 解压结果送去扫描、预览和转码，原文件保留")

            app.encoder.input_var.set("")
            app._download_completed(str(original))
            app.encode_download_btn.invoke()
            assert app.encoder.input_var.get() == str(original)
            assert not app.encoder._pairs
            print("[通过] 文本下载结果可直接转码，旧扫描结果已失效")

            app.tabs.select(app.archive_tab)
            app.archive.open_compress_dialog()
            dialogs = [w for w in app.archive_tab.winfo_children() if isinstance(w, tk.Toplevel)]
            assert len(dialogs) == 1
            dialogs[0].destroy()
            packed = root / "重新打包.zip"
            compress = se.Job(path=str(packed), kind="zip", mode="compress", sources=[str(original)], dest=str(root))
            app.archive.add_compress_job(compress)
            pump(app, lambda: not app.archive.busy())
            assert compress.state == "完成", compress.error
            with zipfile.ZipFile(packed) as zf:
                assert zf.read(zf.namelist()[0]) == raw
            print("[通过] 压缩对话框和后台压缩任务可用")

            # C 盘清理页：注入假目录，扫描 → 勾选 → 清理 → 复扫为 0；不碰真实系统路径。
            clean_root = root / "假垃圾"
            clean_root.mkdir()
            stale = clean_root / "旧临时.bin"
            stale.write_bytes(b"x" * 4096)
            past = time.time() - 3 * 24 * 3600
            os.utime(stale, (past, past))
            fresh = clean_root / "新临时.bin"
            fresh.write_bytes(b"y" * 100)
            app.cleaner.engine = c_cleaner.CleanEngine(
                root_overrides={"user_temp": clean_root}, min_age_hours=24)
            app.tabs.select(app.cleaner.frame)
            app.cleaner.scan_btn.invoke()
            pump(app, lambda: not app.cleaner.busy()
                 and app.cleaner.clean_btn.instate(["!disabled"]))
            assert app.cleaner._scans["user_temp"].bytes == 4096
            for var in app.cleaner.check_vars.values():
                var.set(False)
            app.cleaner.check_vars["user_temp"].set(True)
            app.cleaner._confirm_clean = lambda *args, **kwargs: True
            popups = []
            real_showinfo = tkinter.messagebox.showinfo
            tkinter.messagebox.showinfo = lambda *a, **k: popups.append(a)
            try:
                app.cleaner.clean_btn.invoke()
                pump(app, lambda: not app.cleaner.busy()
                     and app.cleaner.clean_btn.instate(["!disabled"]))
            finally:
                tkinter.messagebox.showinfo = real_showinfo
            assert not stale.exists(), "超过 24 小时的假垃圾应被清理"
            assert fresh.exists(), "24 小时内的文件必须保留"
            assert app.cleaner._scans["user_temp"].bytes == 0
            assert popups, "清理完成后应弹出结果明细"
            assert not errors, errors
            print("[通过] C 盘清理：扫描统计、24 小时保护、勾选清理后复扫为 0")

            # 只在本次测试的临时目录里改名，覆盖预览、取消确认、执行和撤销。
            rename_root = root / "重命名样本"
            rename_root.mkdir()
            (rename_root / "a.txt").write_bytes(b"first")
            (rename_root / "b.txt").write_bytes(b"second")
            (rename_root / "keep.bin").write_bytes(b"untouched")
            panel = app.renamer
            panel.record_dir = root / "rename_records"
            app.tabs.select(panel)
            panel.set_inputs([rename_root])
            panel.vars["mode"].set("统一名称")
            panel.vars["base_name"].set("文稿")
            panel.vars["extensions"].set("txt")
            panel.numbering_var.set(True)
            panel.preview_btn.invoke()
            pump(app, lambda: not panel.busy() and panel.status_var.get().startswith("预览完成"))
            assert len(panel.rows) == 2
            assert panel.execute_btn.instate(["!disabled"])
            assert (rename_root / "a.txt").exists()
            assert not (rename_root / "文稿_001.txt").exists()
            panel._confirm = lambda *a, **k: False
            panel.execute_btn.invoke()
            assert (rename_root / "a.txt").exists()
            panel._confirm = lambda *a, **k: True
            panel.execute_btn.invoke()
            pump(app, lambda: not panel.busy() and panel.status_var.get().startswith("重命名完成"))
            assert (rename_root / "文稿_001.txt").read_bytes() == b"first"
            assert (rename_root / "文稿_002.txt").read_bytes() == b"second"
            assert (rename_root / "keep.bin").read_bytes() == b"untouched"
            assert panel.execute_btn.instate(["disabled"])
            panel.undo_btn.invoke()
            pump(app, lambda: not panel.busy() and panel.status_var.get().startswith("撤销完成"))
            assert (rename_root / "a.txt").read_bytes() == b"first"
            assert (rename_root / "b.txt").read_bytes() == b"second"
            panel.numbering_var.set(False)
            panel.preview_btn.invoke()
            pump(app, lambda: not panel.busy() and panel.status_var.get().startswith("预览完成"))
            assert all(row.error for row in panel.rows)
            assert panel.execute_btn.instate(["disabled"])
            assert not errors, errors
            print("[通过] 批量重命名：预览不写盘、取消确认、编号改名、原样撤销和冲突阻止")

            storage_root = (root / "空间查重样本").resolve()
            nested = storage_root / "nested"
            nested.mkdir(parents=True)
            old_copy, new_copy = storage_root / "old.txt", nested / "latest.txt"
            old_copy.write_bytes(b"duplicate")
            new_copy.write_bytes(b"duplicate")
            old_time = time.time() - 3600
            os.utime(old_copy, (old_time, old_time))
            (storage_root / "different.txt").write_bytes(b"something")
            (storage_root / "big.bin").write_bytes(b"x" * 4096)
            total_size = 4096 + 3 * len(b"duplicate")
            app.tabs.select(app.space)
            app.space.set_root(storage_root)
            app.space.scan_btn.invoke()
            pump(app, lambda: not app.space._active and app.space.status_var.get().startswith("扫描完成"))
            assert app.space.scan.total_size == total_size
            first = app.space.tree.get_children()[0]
            assert app.space._rows[first].path.name == "big.bin"
            assert app.space.scan_btn.instate(["!disabled"])
            app.space.limit_var.set("2")
            assert len(app.space.tree.get_children()) == 2
            space_csv = root / "space_report.csv"
            with patch("storage_gui.filedialog.asksaveasfilename", return_value=str(space_csv)):
                app.space.export_btn.invoke()
            exported = list(csv.reader(space_csv.read_text(encoding="utf-8-sig").splitlines()))
            assert any(str(new_copy) in row for row in exported), "导出应包含显示上限之外的文件"
            app.space.limit_var.set("200")
            app.space.duplicates_btn.invoke()
            assert app.tabs.select() == str(app.duplicates)
            dup = app.duplicates
            pump(app, lambda: not dup._active and dup.status_var.get().startswith("扫描完成"))
            assert len(dup.duplicates.groups) == 1
            group = dup.duplicates.groups[0]
            assert group.keeper.path == new_copy
            assert not dup.checked
            dup.limit_var.set("1000")
            assert len(dup.duplicates.groups) == 1, "改变显示上限不应使查重结果失效"
            dup_csv = root / "duplicate_report.csv"
            with patch("storage_gui.filedialog.asksaveasfilename", return_value=str(dup_csv)):
                dup.export_btn.invoke()
            assert group.digest in dup_csv.read_text(encoding="utf-8-sig")
            dup.suggest_btn.invoke()
            assert dup.checked == {storage_tools.key(old_copy)}
            assert dup.delete_btn.instate(["!disabled"])
            keeper_row = next(iid for iid, (file, _group) in dup._rows.items() if file.path == new_copy)
            popups = []
            real_showinfo = tkinter.messagebox.showinfo
            tkinter.messagebox.showinfo = lambda *a, **k: popups.append(a)
            try:
                dup.toggle_checked([keeper_row])
                assert dup.checked == {storage_tools.key(old_copy)}, "不能勾选最后一份副本"
                assert popups
                dup._confirm_delete = lambda *_: False
                dup.delete_btn.invoke()
                assert old_copy.exists() and new_copy.exists()
                recycle_folder = root / "模拟回收站"
                recycle_folder.mkdir()
                def recycle_storage(paths):
                    for index, path in enumerate(paths):
                        os.rename(path, recycle_folder / f"{index}_{path.name}")
                dup.recycler = recycle_storage
                dup._confirm_delete = lambda *_: True
                dup.delete_btn.invoke()
                pump(app, lambda: not dup._active and dup.status_var.get().startswith("系统操作结束"))
                assert not old_copy.exists()
                assert new_copy.read_bytes() == b"duplicate"
                assert (storage_root / "different.txt").read_bytes() == b"something"
                assert not dup.tree.get_children(), "处理后旧结果应失效"
            finally:
                tkinter.messagebox.showinfo = real_showinfo
            app.tabs.select(app.space)
            app.space.scan_btn.invoke()
            pump(app, lambda: not app.space._active and app.space.status_var.get().startswith("扫描完成"))
            assert app.space.scan.total_size == total_size - len(b"duplicate")
            folder_row = next(iid for iid, entry in app.space._rows.items() if isinstance(entry, storage_tools.FolderInfo))
            app.space.tree.selection_set(folder_row)
            app.space.drill_btn.invoke()
            pump(app, lambda: not app.space._active and app.space.status_var.get().startswith("扫描完成"))
            assert app.space.scan.root == nested
            assert app.space.scan.total_size == len(b"duplicate")
            assert not errors, errors
            print("[通过] 空间分析与查重：大小排序、目录下钻、完整哈希、建议保留最新、取消确认及回收副本保护")

            shell_text = root / "右键 文件 & 感叹! 百分%.TXT"
            shell_text.write_text("MZ 开头的普通 TXT，右键也应进入转码页。", encoding="utf-8")
            app.encoder.input_var.set("")
            app.open_inputs([str(shell_text)])
            assert app.tabs.select() == str(app.encoder)
            assert app.encoder.input_var.get() == str(shell_text)
            assert not (app.encoder._worker and app.encoder._worker.is_alive())
            shell_zip = root / "右键 压缩包 & 感叹! 百分%.ZIP"
            shell_zip.write_bytes(packed.read_bytes())
            app.open_inputs([str(shell_zip)])
            assert app.tabs.select() == str(app.archive_tab)
            assert app.archive.jobs[-1].path == str(shell_zip)
            assert app.archive.jobs[-1].state == "等待"
            split = root / "右键 分卷.zip.001"
            split.write_bytes(b"split first volume")
            app.open_inputs([str(split)])
            assert app.tabs.select() == str(app.archive_tab)
            assert app.archive.jobs[-1].path == str(split)
            print("[通过] 右键输入按类型进入对应页，保留中文、空格和特殊符号，分卷进入解压页")

            app.download.name_var.set("关闭时暂停.zip")
            app.download.url_var.set(base + "/slow")
            app.download.start_btn.invoke()
            pump(app, lambda: app.download.job.part is not None and Path(app.download.job.part).exists())
            assert not errors, errors
            active = app.download.job
            app.request_close()
            deadline = time.monotonic() + 30
            while app.busy() and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            app.update()
            assert not app.busy()
            assert active.cancel_event.is_set()
            assert (root / "settings.json").exists()
            assert not errors, errors
            print("[通过] 关闭窗口时停止任务、保存设置，无 Tk 回调异常")
        finally:
            # 断言失败也先停止所有线程，避免后台任务占用临时目录。
            try:
                if app.winfo_exists():
                    app.request_close()
                    deadline = time.monotonic() + 30
                    while app.busy() and time.monotonic() < deadline:
                        app.update()
                        time.sleep(0.01)
                    app.update()
            except tk.TclError:
                pass
    print("工具箱图形界面冒烟测试全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
