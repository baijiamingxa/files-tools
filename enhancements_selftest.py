#!/usr/bin/env python3
"""第二档功能离线回归：临时文件、本地 IPC、模拟版本接口。"""
from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_support as desktop
import download_queue as queue_core
import file_tools as core
from txt_encoder_core import make_console_tolerant


class EnhancementTests(unittest.TestCase):
    def test_literal_replace_at_every_chunk_boundary(self):
        sample = "ababa中\r\nxxababa\r\nababa" * 4
        for encoding, bom in (("utf-8", b""), ("utf-8", codecs.BOM_UTF8), ("gbk", b""), ("big5", b""), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE)):
            for size in (1, 3, 8, 31):
                with self.subTest(encoding=encoding, chunk=size), tempfile.TemporaryDirectory() as temp, patch.object(core, "CHUNK", size):
                    source = Path(temp) / "原文.txt"
                    raw = bom + sample.encode(encoding)
                    source.write_bytes(raw)
                    options = core.ReplaceOptions("ababa", "文字", encoding)
                    rows = core.preview_replacements([source], options)
                    self.assertFalse(rows[0].error, rows[0].error)
                    self.assertEqual(rows[0].count, sample.count("ababa"))
                    self.assertEqual(source.read_bytes(), raw)
                    core.execute_replacements(rows, options)
                    self.assertEqual(rows[0].status, "已替换", rows[0].error)
                    self.assertEqual(rows[0].destination.read_bytes(), bom + sample.replace("ababa", "文字").encode(encoding))
                    self.assertEqual(source.read_bytes(), raw)

    def test_inplace_backup_and_multiline_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "资料.txt"
            raw = "第一行\r\n删除\r\n保留\r\n".encode("utf-8-sig")
            source.write_bytes(raw)
            options = core.ReplaceOptions("删除\r\n", "", inplace=True)
            rows = core.preview_replacements([source], options)
            core.execute_replacements(rows, options)
            self.assertEqual(rows[0].status, "已替换", rows[0].error)
            self.assertEqual(Path(rows[0].backup).read_bytes(), raw)
            self.assertEqual(source.read_bytes().decode("utf-8-sig"), "第一行\r\n保留\r\n")

    def test_unrepresentable_and_binary_and_no_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "gbk.txt"
            path.write_bytes("中文文稿".encode("gbk"))
            options = core.ReplaceOptions("中文", "😀", "gbk")
            row = core.preview_replacements([path], options)[0]
            self.assertTrue(row.error)
            self.assertFalse(row.destination.exists())
            options = core.ReplaceOptions("不存在", "替换", "gbk")
            row = core.preview_replacements([path], options)[0]
            self.assertEqual(row.count, 0)
            core.execute_replacements([row], options)
            self.assertFalse(row.destination.exists())
            path.write_bytes(b"a\x00b")
            self.assertTrue(core.preview_replacements([path], core.ReplaceOptions("a", "b", "utf-8"))[0].error)

    def test_stale_content_restored_timestamp_and_changed_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.txt"
            path.write_bytes(b"old old")
            options = core.ReplaceOptions("old", "new", inplace=True)
            rows = core.preview_replacements([path], options)
            stat = path.stat()
            path.write_bytes(b"bad bad")
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            core.execute_replacements(rows, options)
            self.assertEqual(rows[0].status, "失败")
            self.assertEqual(path.read_bytes(), b"bad bad")
            rows = core.preview_replacements([path], options)
            different = core.ReplaceOptions("bad", "new", inplace=False)
            # 选择真正命中的计划验证规则改变被拒绝。
            rows = core.preview_replacements([path], different)
            core.execute_replacements(rows, core.ReplaceOptions("bad", "xxx", inplace=False))
            self.assertEqual(rows[0].status, "失败")
            self.assertEqual(path.read_bytes(), b"bad bad")

    def test_output_race_and_cancel_preserve_original(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.txt"
            path.write_bytes(b"old " * 100)
            options = core.ReplaceOptions("old", "new")
            rows = core.preview_replacements([path], options)
            rows[0].destination.write_bytes(b"other process")
            core.execute_replacements(rows, options)
            self.assertEqual(rows[0].status, "失败")
            self.assertEqual(rows[0].destination.read_bytes(), b"other process")
            rows[0].destination.unlink()
            rows = core.preview_replacements([path], options)
            class Stop:
                calls = 0
                def is_set(self):
                    self.calls += 1
                    return self.calls > 8
            with patch.object(core, "CHUNK", 3), self.assertRaises(core.FileToolCancelled):
                core.execute_replacements(rows, options, Stop())
            self.assertEqual(path.read_bytes(), b"old " * 100)
            self.assertEqual(sorted(item.name for item in Path(temp).iterdir()), ["source.txt"])

    def test_hashes_and_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file.bin"
            raw = bytes(range(256)) * 50
            path.write_bytes(raw)
            digests = core.file_hashes(path)
            self.assertEqual(digests["md5"], hashlib.md5(raw).hexdigest())
            self.assertEqual(digests["sha256"], hashlib.sha256(raw).hexdigest())
            changed = []
            def progress(done, total):
                if not changed:
                    path.write_bytes(b"new contents")
                    changed.append(True)
            with patch.object(core, "CHUNK", 128), self.assertRaises((core.FileToolError, OSError)):
                core.file_hashes(path, progress=progress)

    def test_checksum_manifests_and_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "空格 中文.bin"
            path.write_bytes(b"known content")
            rows = core.calculate_hashes([path])
            manifest = root / "checks.csv"
            core.export_hashes(rows, manifest)
            records = core.read_checksum_manifest(manifest)
            self.assertEqual([row[2] for row in core.compare_hashes(rows, records, root)], ["一致", "一致"])
            manifest = root / "checks.sha256"
            manifest.write_text(rows[0].digests["sha256"] + " *空格 中文.bin\n", encoding="utf-8-sig")
            records = core.read_checksum_manifest(manifest)
            self.assertEqual(core.compare_hashes(rows, records, root)[0][2], "一致")
            self.assertEqual(core.compare_hashes(rows, [("../not-selected", "sha256", "0" * 64)], root)[0][2], "未选择此文件")
            self.assertEqual(core.compare_hashes(rows, [(path.name, "sha256", "0" * 64)], root)[0][2], "不一致")
            manifest.write_text("not a checksum", encoding="utf-8")
            with self.assertRaises(core.FileToolError):
                core.read_checksum_manifest(manifest)
            manifest.write_text("0" * 32 + "  a.txt\n" + "1" * 32 + "  a.txt", encoding="utf-8")
            with self.assertRaises(core.FileToolError):
                core.read_checksum_manifest(manifest)

    def test_queue_import_persistence_and_invalid_records(self):
        links = queue_core.parse_links("# 注释\nhttps://example.test/a?x=1&y=2\nhttp://example.test/b\nhttps://example.test/a?x=1&y=2")
        self.assertEqual(len(links), 2)
        for invalid in ("file:///secret", "https://", "https://example.test/a b"):
            with self.assertRaises(ValueError):
                queue_core.parse_links(invalid)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            text = root / "链接.txt"
            text.write_bytes(("# 下载链接\r\n" + "\r\n".join(links)).encode("gbk"))
            self.assertEqual(queue_core.import_links(text), links)
            item = queue_core.QueueItem(links[0], str(root), status="下载中", path=str(root / "result.bin"))
            path = root / "queue.json"
            queue_core.save_queue([item], path)
            loaded = queue_core.load_queue(path)
            self.assertEqual(loaded[0].status, "已暂停")
            self.assertEqual(loaded[0].identity, item.identity)
            self.assertEqual(loaded[0].path, item.path)
            path.write_text('{"invalid":"format"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                queue_core.load_queue(path)

    def test_single_instance_handoff_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            first = desktop.SingleInstance(temp)
            second = desktop.SingleInstance(temp)
            try:
                self.assertTrue(first.primary)
                self.assertFalse(second.primary)
                inputs = [str(Path(temp) / "中文 & % !.txt"), "https://example.test/a?x=1&y=2"]
                second.forward(inputs)
                self.assertEqual(first.messages.get(timeout=2), inputs)
                second.forward([])
                self.assertEqual(first.messages.get(timeout=2), [])
                # 无令牌的本地连接不能向主窗口注入文件列表。
                metadata = json.loads(first.endpoint.read_text(encoding="utf-8"))
                with socket.create_connection(("127.0.0.1", metadata["port"]), timeout=2) as connection:
                    connection.sendall(b'{"token":"wrong","inputs":[]}\n')
                    self.assertFalse(desktop._receive(connection)["ok"])
            finally:
                second.close()
                first.close()
            third = desktop.SingleInstance(temp)
            try:
                self.assertTrue(third.primary)
            finally:
                third.close()

    def test_release_versions_and_rejected_redirect(self):
        self.assertEqual(desktop.version_tuple("txt-encoder-v1.0.0"), (1, 0, 0))
        self.assertEqual(desktop.version_tuple("txt编码器v1.1.0"), (1, 1, 0))
        self.assertEqual(desktop.version_tuple("文件工具箱-v1.2.3"), (1, 2, 3))
        self.assertIsNone(desktop.version_tuple("v1.2.0-beta"))
        class Reply(io.BytesIO):
            pass
        def reply(tag="v1.4.0", url=None, prerelease=False):
            return Reply(json.dumps({"tag_name": tag, "html_url": url or desktop.RELEASES_URL + "/tag/" + tag, "body": "新版本说明", "prerelease": prerelease}).encode())
        data = desktop.check_release(opener=lambda *a, **k: reply())
        self.assertTrue(data["new"])
        self.assertFalse(desktop.check_release(opener=lambda *a, **k: reply("v1.0.0"))["new"])
        self.assertFalse(desktop.check_release(opener=lambda *a, **k: reply("v1.1.0"))["new"])
        for result in (reply(url="https://elsewhere.test/download"), reply(prerelease=True), Reply(b"not json")):
            with self.assertRaises(ValueError):
                desktop.check_release(opener=lambda *a, **k: result)


if __name__ == "__main__":
    make_console_tolerant()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EnhancementTests))
    if result.wasSuccessful():
        print("第二档增强自检全部通过 ✔")
    raise SystemExit(0 if result.wasSuccessful() else 1)
