#!/usr/bin/env python3
"""真实创建工具箱，走完下载→解压→预览→转码并检查原文件。"""

from __future__ import annotations

import io
import os
import tempfile
import time
import tkinter as tk
import tkinter.messagebox
import zipfile
from pathlib import Path

import c_cleaner
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
            assert len(app.tabs.tabs()) == 4
            # 所有工具共用一个 Tk 解释器，没有额外主窗口。
            assert app.encoder.tk is app.download.root.tk is app.archive_tab.tk
            print("[通过] 四个工具嵌入同一个窗口")

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
