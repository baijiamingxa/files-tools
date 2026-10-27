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
import runtime_manager
import runtime_gui
from dependency_check import DependencyResult
import update_gui
import document_conversion
from document_selftest import docx_fixture, fake_worker
from download_queue import load_queue
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
        app = ToolboxApp(config_path=root / "settings.json", startup_check=False)
        errors = []
        def report_error(kind, value, trace):
            errors.append(f"{kind.__name__}: {value}")
        app.report_callback_exception = report_error
        try:
            app.update()
            assert len(app.tabs.tabs()) == 10
            assert sorted(image.width() for image in app._icon_images) == [32, 64]
            # 所有工具共用一个 Tk 解释器，没有额外主窗口。
            assert app.encoder.tk is app.download.root.tk is app.archive_tab.tk
            print("[通过] 十个工具嵌入同一个窗口")

            # 环境界面使用模拟检测，不访问外网，也不写真实运行组件目录。
            def ready_runtime(stop, progress, **kwargs):
                progress("模拟组件检查")
                return [("测试扩展组件", "可用", "测试目录中的模拟组件")]
            with patch.object(runtime_manager, "configure", side_effect=ready_runtime) as configure:
                simulated = [DependencyResult("py7zr", "7z 压缩解压", "未安装（可选）", detail="可使用便携 7-Zip。"),
                             DependencyResult("zstandard", "Zstd 压缩解压", "可用", "test-1", "测试组件目录", "样本测试通过。")]
                with patch.object(runtime_gui, "inspect_dependencies", return_value=simulated), patch.object(runtime_manager, "download_verified", side_effect=AssertionError("手动自检不能下载")):
                    assert app.selfcheck_btn.cget("text") == "自检"
                    app.selfcheck_btn.invoke()
                    pump(app, lambda: not app.runtime.busy())
                    assert configure.call_args.kwargs["auto_install"] is False
                    assert "全部就绪" in app.runtime.message.get()
                    assert "基础运行环境" in app.runtime.detail.get("1.0", "end")
                    assert "可用" in app.runtime.detail.get("1.0", "end")
                    assert "py7zr" in app.runtime.detail.get("1.0", "end")
                    table = [app.runtime.tree.item(item, "values") for item in app.runtime.tree.get_children()]
                    assert any(row[0] == "py7zr" and row[1] == "未安装（可选）" for row in table)
                    assert any(row[0] == "zstandard" and row[2] == "test-1" for row in table)
                    app.runtime.copy_report()
                    assert "py7zr" in app.clipboard_get() and "基础运行环境" in app.clipboard_get()
                broken = [DependencyResult("pyzipper", "AES ZIP", "自检失败", detail="模拟依赖损坏")]
                with patch.object(runtime_gui, "inspect_dependencies", return_value=broken):
                    app.runtime.check_btn.invoke()
                    pump(app, lambda: not app.runtime.busy())
                    assert "1 个依赖加载或自检失败" in app.runtime.message.get()
                    assert "模拟依赖损坏" in app.runtime.detail.get("1.0", "end")
            with patch.object(runtime_manager, "configure", side_effect=OSError("模拟断网")):
                app.runtime.retry_btn.invoke()
                pump(app, lambda: not app.runtime.busy())
                assert "模拟断网" in app.runtime.detail.get("1.0", "end")
                assert str(app.runtime.retry_btn.cget("state")) == "normal"
            def waiting_runtime(stop, progress, **kwargs):
                progress("等待取消")
                stop.wait(10)
                raise runtime_manager.RuntimeCancelled("模拟取消")
            with patch.object(runtime_manager, "configure", side_effect=waiting_runtime):
                app.selfcheck_btn.invoke()
                worker = app.runtime.worker
                app.selfcheck_btn.invoke()
                assert app.runtime.worker is worker, "自检繁忙时不能重复启动检查线程"
                app.runtime.cancel()
                pump(app, lambda: not app.runtime.busy())
                assert "取消" in app.runtime.message.get()
            app.runtime.window.destroy()
            with patch.object(runtime_manager, "configure", side_effect=ready_runtime):
                app.runtime.show()
                app.runtime.retry_btn.invoke()
                pump(app, lambda: not app.runtime.busy())
                assert "全部就绪" in app.runtime.message.get()
                app.runtime.window.destroy()
            print("[通过] 右上角自检：逐项依赖状态、版本和位置、复制报告、只检查不下载、异常提示与取消重试")

            # 文本替换：GBK 文件预览不写盘，取消确认后不生成副本，执行保留编码。
            replace_source = root / "替换样本.txt"
            replace_raw = ("工具箱批量替换测试，旧名称需要更新。\r\n" * 20).encode("gbk")
            replace_source.write_bytes(replace_raw)
            replace_panel = app.replacer
            app.tabs.select(replace_panel)
            replace_panel.set_inputs([replace_source])
            replace_panel.encoding_var.set("GBK (兼容 GB2312)")
            replace_panel.find_text.insert("1.0", "旧名称")
            replace_panel.replace_text.insert("1.0", "新名称")
            app.update()
            replace_panel.scan_btn.invoke()
            pump(app, lambda: not replace_panel.busy())
            assert len(replace_panel.rows) == 1 and replace_panel.rows[0].count == 20
            assert not replace_panel.rows[0].error, replace_panel.rows[0].error
            assert replace_source.read_bytes() == replace_raw
            with patch("tkinter.messagebox.askyesno", return_value=False):
                replace_panel.execute_btn.invoke()
            assert not replace_panel.rows[0].destination.exists()
            with patch("tkinter.messagebox.askyesno", return_value=True):
                replace_panel.execute_btn.invoke()
                pump(app, lambda: not replace_panel.busy())
            replace_output = replace_panel.rows[0].destination
            assert replace_output.read_bytes() == replace_raw.decode("gbk").replace("旧名称", "新名称").encode("gbk")
            assert replace_source.read_bytes() == replace_raw
            replace_panel.replace_text.insert("end", "改动规则")
            app.update()
            assert not replace_panel.rows
            print("[通过] 文本替换：GBK 预览、确认取消、生成副本、保留编码换行及规则失效")

            checks = app.checksums
            app.tabs.select(checks)
            checks.set_inputs([replace_source, replace_output])
            checks.calculate_btn.invoke()
            pump(app, lambda: not checks.busy())
            assert len(checks.rows) == 2 and all(len(row.digests["sha256"]) == 64 for row in checks.rows)
            manifest = root / "校验清单.csv"
            checks.export(manifest)
            checks.verify(manifest)
            pump(app, lambda: not checks.busy())
            assert len(checks.comparisons) == 4 and all(item[2] == "一致" for item in checks.comparisons)
            replace_output.write_bytes(b"changed after hashing")
            checks.verify(manifest)
            pump(app, lambda: not checks.busy())
            assert sum(item[2] == "不一致" for item in checks.comparisons) == 2
            print("[通过] 文件校验：双算法计算、CSV 导出导入、重新计算检测到内容变化")

            # 文档页在 CI 中模拟引擎，只检查真实预览、保护与结果发布；真实引擎另测。
            word = root / "文档 中文 &.docx"
            pdf = root / "文档示例.pdf"
            docx_fixture(word)
            pdf.write_bytes(b"%PDF-fixture")
            word_before, pdf_before = word.read_bytes(), pdf.read_bytes()
            app.open_inputs([str(word), str(pdf)])
            assert app.tabs.select() == str(app.documents)
            panel = app.documents
            panel.scan_btn.invoke()
            pump(app, lambda: not panel.busy())
            assert len(panel.rows) == 2 and len(panel.checked) == 2
            with patch("tkinter.messagebox.askyesno", return_value=False):
                panel.convert_btn.invoke()
            assert not any(row.destination.exists() for row in panel.rows)
            with patch("tkinter.messagebox.askyesno", return_value=True), patch("document_runtime.ensure_office", return_value=root / "fake-office.exe"), patch.object(document_conversion, "run_document_worker", side_effect=fake_worker):
                panel.convert_btn.invoke()
                pump(app, lambda: not panel.busy())
            assert all(row.status == "已完成" for row in panel.rows), [(row.status, row.error) for row in panel.rows]
            assert all(row.destination.is_file() for row in panel.rows)
            assert word.read_bytes() == word_before and pdf.read_bytes() == pdf_before
            panel.scan_btn.invoke()
            pump(app, lambda: not panel.busy())
            assert all(row.error for row in panel.rows), "已有结果不得覆盖"
            print("[通过] Word / PDF 页：右键分派、双向预览、取消确认、后台发布、源文件和重名保护")

            with patch.object(update_gui, "check_release", return_value={"new": True, "version": "v9.0.0", "url": "https://github.com/baijiamingxa/txt-encoder/releases/tag/v9.0.0", "notes": "模拟更新说明"}):
                app.updater.show()
                app.updater.start()
                pump(app, lambda: not app.updater.busy())
                assert "v9.0.0" in app.updater.button.cget("text")
                assert "模拟更新说明" in app.updater.notes.get("1.0", "end")
                app.updater.automatic()
                assert not app.updater.busy(), "当天已检查后不重复访问接口"
            with patch.object(update_gui, "check_release", side_effect=OSError("模拟更新检查断网")):
                app.updater.start()
                pump(app, lambda: not app.updater.busy())
                assert "断网" in app.updater.message.get()
            app.updater.window.destroy()
            print("[通过] 更新提醒：新版本、说明、当天缓存及断网可重试；未访问外网")

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

            # 顺序队列复用真实本地下载服务；重名自动避让，暂停后按原路径续传。
            queue_dir = root / "队列下载"
            app.tabs.select(app.download_tab)
            downloader = app.download
            downloader.dir_var.set(str(queue_dir))
            downloader.threads_var.set(1)
            downloader.name_var.set("队列不使用此单任务名称.zip")
            downloader.queue_text.insert("1.0", base + "/range?first\n" + base + "/range?second\n" + base + "/stream")
            downloader.add_queue_text()
            assert len(downloader.queue_items) == 3
            assert not queue_dir.exists(), "添加队列不能触发下载或写出文件"
            downloader.start_queue()
            pump(app, lambda: not downloader.busy(), timeout=40)
            assert all(item.status == "完成" for item in downloader.queue_items), [(item.status, item.error) for item in downloader.queue_items]
            assert len({item.path for item in downloader.queue_items}) == 3
            assert all(Path(item.path).read_bytes() == archive_bytes.getvalue() for item in downloader.queue_items)
            downloader.queue_tree.selection_set(downloader.queue_items[0].identity)
            downloader.use_queue_result()
            assert app.last_download == Path(downloader.queue_items[0].path)
            slow_list = root / "下载链接.txt"
            slow_list.write_bytes(("# 下载队列\r\n" + base + "/slow").encode("gbk"))
            downloader.import_queue(slow_list)
            downloader.start_queue()
            pump(app, lambda: downloader.job is not None and downloader.job.url.endswith("/slow") and downloader.job.done_bytes() > 0)
            downloader.pause_queue()
            pump(app, lambda: not downloader.busy())
            paused = downloader.queue_items[-1]
            assert paused.status == "已暂停", paused.status
            downloader.start_queue()
            pump(app, lambda: not downloader.busy(), timeout=40)
            assert paused.status == "完成" and Path(paused.path).read_bytes() == archive_bytes.getvalue()
            assert len(load_queue(downloader.queue_path)) == 4
            protected = queue_dir / "已有文件.bin"
            protected.write_bytes(b"keep original")
            downloader.add_links([base + "/conflict", base + "/range?third"])
            downloader.queue_items[-2].path = str(protected)
            downloader.start_queue()
            pump(app, lambda: not downloader.busy(), timeout=40)
            assert downloader.queue_items[-2].status == "失败"
            assert downloader.queue_items[-1].status == "完成"
            assert protected.read_bytes() == b"keep original"
            print("[通过] 批量队列：多行 / TXT 导入、顺序下载、重名避让、暂停续传、持久化及失败后继续")

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

            app.download.clear_completed()
            app.download.add_links([base + "/slow"])
            app.download.start_queue()
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
            saved_queue = load_queue(app.download.queue_path)
            assert saved_queue[-1].status in ("已暂停", "完成")
            assert saved_queue[-1].path == active.save_path
            assert (root / "settings.json").exists()
            assert not errors, errors
            print("[通过] 关闭窗口时暂停队列、保存断点与最终状态、保存设置，无 Tk 回调异常")
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
