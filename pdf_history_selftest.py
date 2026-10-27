#!/usr/bin/env python3
"""PDF 处理、持久任务记录与保护检查；--real 检查真实 PDF 引擎。"""
from __future__ import annotations

import csv
import hashlib
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pdf_tools as pdf
from file_tools import FileToolCancelled
from task_history import HistoryStore, HistoryRecorder, tracked, item, file_rows


def fake_worker(job, workdir, *args, **kwargs):
    if job["mode"] == "pdf_info":
        if Path(job["source"]).read_bytes().startswith(b"bad"):
            raise ValueError("PDF 已损坏")
        return {"ok": True, "pages": 5}
    for path in job["outputs"]:
        Path(path).write_bytes(b"%PDF-generated")
    return {"ok": True, "detail": "模拟处理完成"}


class PdfTests(unittest.TestCase):
    def test_page_ranges(self):
        self.assertEqual(pdf.parse_pages("3,1-2，5", 5), [2, 0, 1, 4])
        self.assertEqual(pdf.parse_pages("", 3), [0, 1, 2])
        for text in ("0", "6", "2-1", "1,,2", "1,1", "1-3,2", "-1", "a", "1-", "1.5"):
            with self.assertRaises(pdf.FileToolError, msg=text):
                pdf.parse_pages(text, 5)

    def test_order_splits_and_collisions(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pdf, "run_document_worker", side_effect=fake_worker):
            root = Path(temp)
            first, second = root / "甲.pdf", root / "乙.pdf"
            first.write_bytes(b"%PDF-first")
            second.write_bytes(b"%PDF-second")
            options = pdf.PdfOptions()
            rows = pdf.build_pdf_plan([second, first], options)
            self.assertEqual([part.path for part in rows[0].sources], [second, first])
            split = pdf.build_pdf_plan([first], replace(options, mode="split", split_every=2))[0]
            self.assertEqual(split.selections, ((0, 1), (2, 3), (4,)))
            self.assertEqual(len(split.destinations), 3)
            split.destinations[0].parent.mkdir()
            split.destinations[0].write_bytes(b"keep")
            self.assertTrue(pdf.build_pdf_plan([first], replace(options, mode="split", split_every=2))[0].error)
            other = root / "other"
            other.mkdir()
            same = other / first.name
            same.write_bytes(first.read_bytes())
            rows = pdf.build_pdf_plan([first, same], replace(options, mode="extract", output_dir=str(root / "out")))
            self.assertTrue(all(row.error for row in rows))

    def test_bad_merge_is_not_partial(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pdf, "run_document_worker", side_effect=fake_worker):
            root = Path(temp)
            good, bad = root / "good.pdf", root / "bad.pdf"
            good.write_bytes(b"%PDF-good")
            bad.write_bytes(b"bad document")
            rows = pdf.build_pdf_plan([good, bad], pdf.PdfOptions())
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].error)
            self.assertFalse(rows[0].destinations)

    def test_stale_source_and_publish_race(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pdf, "run_document_worker", side_effect=fake_worker):
            source = Path(temp) / "源.pdf"
            source.write_bytes(b"%PDF-first")
            options = pdf.PdfOptions(mode="rotate")
            rows = pdf.build_pdf_plan([source], options)
            info = source.stat()
            source.write_bytes(b"%PDF-other")
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
            pdf.execute_pdf_plan(rows, options)
            self.assertEqual(rows[0].status, "失败")
            self.assertFalse(rows[0].destinations[0].exists())
            rows = pdf.build_pdf_plan([source], options)
            def race(job, work, *args, **kwargs):
                result = fake_worker(job, work, *args, **kwargs)
                rows[0].destinations[0].write_bytes(b"outside")
                return result
            with patch.object(pdf, "run_document_worker", side_effect=race):
                pdf.execute_pdf_plan(rows, options)
            self.assertEqual(rows[0].destinations[0].read_bytes(), b"outside")
            self.assertEqual(rows[0].outputs, [])
            self.assertEqual(source.read_bytes(), b"%PDF-other")

    def test_partial_split_and_cancelled_outputs_are_truthful(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(pdf, "run_document_worker", side_effect=fake_worker):
            source = Path(temp) / "源.pdf"
            source.write_bytes(b"%PDF-original")
            options = pdf.PdfOptions(mode="split", split_every=2)
            rows = pdf.build_pdf_plan([source], options)
            publish = pdf._publish_new
            def publish_race(src, dst):
                publish(src, dst)
                rows[0].destinations[1].write_bytes(b"outside")
            with patch.object(pdf, "_publish_new", side_effect=publish_race):
                pdf.execute_pdf_plan(rows, options)
            self.assertEqual(rows[0].status, "失败")
            self.assertEqual(rows[0].outputs, [rows[0].destinations[0]])
            self.assertEqual(rows[0].destinations[1].read_bytes(), b"outside")
            self.assertEqual([row["state"] for row in pdf.history_rows(rows)], ["已完成", "失败"])
            options = replace(options, output_dir=str(Path(temp) / "cancelled"))
            rows = pdf.build_pdf_plan([source], options)
            stop = threading.Event()
            def cancel(job, work, *args, **kwargs):
                result = fake_worker(job, work, *args, **kwargs)
                stop.set()
                return result
            with patch.object(pdf, "run_document_worker", side_effect=cancel), self.assertRaises(FileToolCancelled):
                pdf.execute_pdf_plan(rows, options, stop)
            self.assertEqual(rows[0].status, "已取消")
            self.assertFalse(any(path.exists() for path in rows[0].destinations))


class HistoryTests(unittest.TestCase):
    def test_persist_search_recover_and_do_not_delete_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "个人文件.txt"
            source.write_text("must keep")
            store = HistoryStore(root / "history.sqlite3")
            token = store.begin("PDF 提取", [source])
            store.finish(token, [item(source, source, detail="100%_正确")])
            self.assertEqual(len(store.query("100%_")), 1)
            self.assertEqual(len(store.query("100%_x")), 0)
            self.assertEqual(HistoryStore(store.path).query()[0]["status"], "已完成")
            active = store.begin("未结束", [source])
            store.delete()
            self.assertEqual(store.query()[0]["id"], active)
            recovered = HistoryStore(store.path, recover=True)
            self.assertEqual(recovered.query()[0]["status"], "意外中断")
            recovered.delete()
            self.assertEqual(source.read_text(), "must keep")

    def test_concurrent_writers_statuses_privacy_and_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.sqlite3")
            def work(index):
                token = store.begin("并发记录", [f"https://user:password@example.test/a?token=secret#key"])
                store.finish(token, [item("=危险.csv", "output", "已完成"), item("bad", state="失败", detail="错误 https://example.test/a?password=secret")])
            threads = [threading.Thread(target=work, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            records = store.query()
            self.assertEqual(len(records), 12)
            self.assertTrue(all(record["status"] == "部分完成" for record in records))
            output = Path(temp) / "记录.csv"
            store.export(output)
            raw = output.read_text(encoding="utf-8-sig")
            self.assertNotIn("secret", raw)
            self.assertNotIn("password", raw)
            self.assertIn("'=危险.csv", raw)
            token = store.begin("隐私", ["https://user:pass@example.test/path?auth=secret"])
            self.assertEqual(store.query("隐私")[0]["items"][0]["input"], "https://example.test/path")

    def test_recording_failure_does_not_change_work_outcome(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = HistoryRecorder(Path(temp) / "history.sqlite3")
            def failure():
                raise OSError("写入失败")
            with self.assertRaises(OSError):
                tracked(recorder, "失败任务", failure, ["original"])()
            record = recorder.store.query()[0]
            self.assertEqual(record["status"], "失败")
            self.assertEqual(record["items"][0]["input"], "original")
            with patch.object(recorder.store, "begin", side_effect=OSError("磁盘不可写")):
                self.assertEqual(tracked(recorder, "普通任务", lambda: 42)(), 42)
            self.assertIn("磁盘不可写", recorder.error)
            stop = threading.Event()
            def cancelled():
                stop.set()
                raise FileToolCancelled("用户停止")
            with self.assertRaises(FileToolCancelled):
                tracked(recorder, "取消任务", cancelled, ["source"], cancelled=stop.is_set)()
            self.assertEqual(recorder.store.query()[0]["status"], "已取消")

    def test_retention_truncation_and_whitelisted_details(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.sqlite3", keep=2)
            active = store.begin("进行中")
            for index in range(3):
                token = store.begin(f"已结束 {index}")
                store.finish(token, [item("source")])
            records = store.query()
            self.assertEqual(len(records), 3)
            self.assertIn(active, [record["id"] for record in records])
            token = store.begin("大批量")
            store.finish(token, [item("good") for _ in range(5000)] + [item("bad", state="失败")])
            record = store.query("大批量")[0]
            self.assertEqual(store.query("大批量", include_items=False)[0]["items"], [])
            self.assertEqual(len(store.get(token)["items"]), 5000)
            self.assertEqual(record["status"], "部分完成")
            self.assertEqual(len(record["items"]), 5000)
            self.assertIn("失败 1", record["summary"])
            self.assertIn("仅保存前 5000", record["summary"])
            from types import SimpleNamespace
            rows = file_rows([SimpleNamespace(path="source", destination="output", status="已替换", error="", backup="backup", preview="private-body", options={"password": "private-password"})])
            self.assertNotIn("private", str(rows))
            self.assertIn("backup", rows[0]["detail"])


def real_test():
    import pymupdf
    root = Path(__file__).resolve().parent / "_PDF处理验证"
    root.mkdir(exist_ok=True)
    run = root / str(__import__("time").time_ns())
    run.mkdir()
    first, second = run / "中文样本A.pdf", run / "样本B.pdf"
    for path, prefix, count in ((first, "A", 5), (second, "B", 2)):
        with pymupdf.open() as document:
            for number in range(count):
                page = document.new_page()
                page.insert_text((50, 60), f"{prefix}-PAGE-{number + 1}", fontsize=24)
                page.insert_text((50, 100), "中文 PDF 页面验证", fontname="china-s", fontsize=18)
                page.draw_rect(pymupdf.Rect(50, 140, 300, 230), color=(0.2, 0.4, 0.8), width=3)
                if number == 1:
                    page.set_rotation(90)
            document.save(path)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)}
    def execute(paths, **kwargs):
        options = pdf.PdfOptions(output_dir=str(run / kwargs.pop("folder")), **kwargs)
        rows = pdf.build_pdf_plan(paths, options)
        assert all(not row.error for row in rows), [row.error for row in rows]
        pdf.execute_pdf_plan(rows, options)
        assert all(row.status == "已完成" for row in rows), [row.error for row in rows]
        return rows
    rows = execute([second, first], mode="merge", folder="merge")
    with pymupdf.open(rows[0].outputs[0]) as document:
        assert document.page_count == 7
        assert "B-PAGE-1" in document[0].get_text()
        assert "A-PAGE-1" in document[2].get_text()
        assert document[1].rotation == 90
        document[0].get_pixmap().save(str(root / "合并预览.png"))
    rows = execute([first], mode="split", split_every=2, folder="split")
    counts = []
    for path in rows[0].outputs:
        with pymupdf.open(path) as document:
            counts.append(document.page_count)
    assert counts == [2, 2, 1], counts
    rows = execute([first], mode="extract", pages="3,1", folder="extract")
    with pymupdf.open(rows[0].outputs[0]) as document:
        assert document.page_count == 2
        assert "A-PAGE-3" in document[0].get_text()
        assert "A-PAGE-1" in document[1].get_text()
    rows = execute([first], mode="rotate", pages="1-2", rotation=90, folder="rotate")
    with pymupdf.open(rows[0].outputs[0]) as document:
        assert [page.rotation for page in document] == [90, 180, 0, 0, 0]
        document[0].get_pixmap().save(str(root / "旋转预览.png"))
    encrypted = run / "有密码.pdf"
    with pymupdf.open(first) as document:
        document.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="correct")
    assert pdf.build_pdf_plan([encrypted], pdf.PdfOptions(mode="extract", password="wrong"))[0].error
    rows = execute([encrypted], mode="extract", password="correct", pages="1", folder="encrypted")
    with pymupdf.open(rows[0].outputs[0]) as document:
        assert not document.is_encrypted and document.page_count == 1
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items())
    print(f"真实 PDF 合并顺序、拆分、提取、旋转、中文与加密输入验证全部通过：{run}")


if __name__ == "__main__":
    from txt_encoder_core import make_console_tolerant
    make_console_tolerant()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__)))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("PDF 常用操作与任务记录自检全部通过 ✔")
    if "--real" in __import__("sys").argv:
        real_test()
