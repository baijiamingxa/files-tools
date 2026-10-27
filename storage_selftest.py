#!/usr/bin/env python3
"""空间与查重自检：所有文件操作仅在临时目录中，通过注入回收器模拟回收。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
import threading
import time
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

import storage_tools as st
from txt_encoder_core import make_console_tolerant


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "中文 范围 & ! %"
        self.root.mkdir()
        self.bin = self.base / "fake_recycle_bin"
        self.bin.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def recycle(self, paths):
        for index, path in enumerate(paths):
            os.rename(path, self.bin / f"{index}_{path.name}")

    def duplicate_fixture(self):
        old = self.file("旧 副本.txt", b"same content")
        newer = self.file("新 副本.txt", b"same content")
        stamp = time.time() - 3600
        os.utime(old, (stamp, stamp))
        self.file("不同.txt", b"other chars!")  # 同样大小也必须通过完整 SHA 校验。
        self.file("unique_size.bin", b"unique")
        scan = st.scan_tree(self.root)
        return scan, st.find_duplicates(scan), old, newer

    def test_nested_space_totals_and_empty_folders(self):
        self.file("top.bin", b"a" * 7)
        self.file("sub/a.txt", b"b" * 11)
        self.file("sub/deep/b.bin", b"c" * 13)
        (self.root / "empty").mkdir()
        scan = st.scan_tree(self.root)
        self.assertEqual(scan.total_size, 31)
        self.assertEqual(len(scan.files), 3)
        self.assertEqual(scan.folders[st.key(self.root / "sub")].size, 24)
        self.assertEqual(scan.folders[st.key(self.root)].folders, 3)
        self.assertEqual(scan.folders[st.key(self.root / "empty")].size, 0)

    def test_duplicates_use_full_hash_and_keep_latest(self):
        scan, result, old, newer = self.duplicate_fixture()
        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(group.digest, hashlib.sha256(b"same content").hexdigest())
        self.assertEqual(group.keeper.path, newer)
        self.assertEqual({file.path for file in group.files}, {old, newer})
        self.assertEqual(group.potential_size, len(b"same content"))
        self.assertEqual(result.hashed_files, 3)

    def test_unique_sizes_not_hashed_and_empty_option(self):
        self.file("one.bin", b"1")
        self.file("two.bin", b"22")
        self.file("empty1.txt", b"")
        self.file("empty2.txt", b"")
        scan = st.scan_tree(self.root)
        with patch.object(st, "hash_file", wraps=st.hash_file) as hashed:
            self.assertFalse(st.find_duplicates(scan).groups)
            self.assertEqual(hashed.call_count, 0)
        self.assertEqual(len(st.find_duplicates(scan, include_empty=True).groups), 1)

    def test_cancel_does_not_change_files(self):
        file = self.file("a.bin", b"x" * (2 * st.CHUNK))
        stop = threading.Event()
        stop.set()
        with self.assertRaises(st.ScanCancelled):
            st.scan_tree(self.root, stop)
        self.assertTrue(file.exists())
        scan = st.scan_tree(self.root)
        stop.clear()
        with self.assertRaises(st.ScanCancelled):
            st.hash_file(scan.files[0], stop, lambda _bytes: stop.set())
        self.assertEqual(file.stat().st_size, 2 * st.CHUNK)

    def test_access_error_reported_and_folder_delete_blocked(self):
        self.file("blocked/a.txt", b"keep")
        original = os.scandir
        def denied(path):
            if Path(path) == self.root / "blocked":
                raise PermissionError("模拟无权访问")
            return original(path)
        with patch.object(st.os, "scandir", denied):
            scan = st.scan_tree(self.root)
        self.assertEqual(len(scan.errors), 1)
        with self.assertRaises(st.StorageError):
            st.prepare_delete(scan, [self.root / "blocked"])

    def test_links_skipped_and_hardlinks_not_duplicate_copies(self):
        source = self.file("a.txt", b"abc")
        alias = self.root / "alias.txt"
        try:
            os.link(source, alias)
        except OSError as exc:
            self.skipTest(str(exc))
        scan = st.scan_tree(self.root)
        result = st.find_duplicates(scan)
        self.assertFalse(result.groups)
        self.assertEqual(result.hardlink_aliases, 1)
        try:
            os.symlink(source, self.root / "symbolic.txt")
        except OSError:
            return  # Windows 未开启创建符号链接权限时，硬链接断言仍有效。
        scan = st.scan_tree(self.root)
        self.assertEqual(scan.skipped_links, 1)
        self.assertEqual(len(scan.files), 2)

    @unittest.skipUnless(os.name == "nt", "Windows 目录联接")
    def test_junction_skipped_and_parent_recycle_requires_manual_review(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.bin").write_bytes(b"outside content")
        folder = self.root / "folder"
        folder.mkdir()
        self.file("folder/inside.bin", b"inside")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(folder / "junction"), str(outside)], check=True, capture_output=True)
        scan = st.scan_tree(self.root)
        self.assertEqual(scan.skipped_links, 1)
        self.assertEqual(scan.total_size, len(b"inside"))
        with self.assertRaises(st.StorageError):
            st.prepare_delete(scan, [folder])
        self.assertEqual((outside / "keep.bin").read_bytes(), b"outside content")

    def test_unreadable_hash_does_not_become_a_duplicate(self):
        scan, _result, old, _newer = self.duplicate_fixture()
        original = st.hash_file
        def denied(file, *args):
            if file.path == old:
                raise PermissionError("模拟读取占用")
            return original(file, *args)
        with patch.object(st, "hash_file", denied):
            result = st.find_duplicates(scan)
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(result.groups)
        self.assertTrue(old.exists())

    def test_cannot_delete_entire_group_or_outside_scope(self):
        scan, result, old, newer = self.duplicate_fixture()
        with self.assertRaises(st.StorageError):
            st.delete_selected(scan, [old, newer], result.groups, recycler=self.recycle)
        with self.assertRaises(st.StorageError):
            st.prepare_delete(scan, [self.base])
        with self.assertRaises(st.StorageError):
            st.prepare_delete(scan, [scan.root])
        self.assertTrue(old.exists() and newer.exists())

    def test_changed_kept_copy_blocks_recycling(self):
        scan, result, old, newer = self.duplicate_fixture()
        before = newer.stat()
        newer.write_bytes(b"changed text")
        os.utime(newer, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(st.StorageError):
            st.delete_selected(scan, [old], result.groups, recycler=self.recycle)
        self.assertTrue(old.exists())

    def test_recycle_one_copy_preserves_keeper(self):
        scan, result, old, newer = self.duplicate_fixture()
        deleted = st.delete_selected(scan, [old], result.groups, recycler=self.recycle)
        self.assertEqual(deleted.removed, [old])
        self.assertFalse(deleted.remaining)
        self.assertEqual(newer.read_bytes(), b"same content")
        self.assertEqual(next(self.bin.iterdir()).read_bytes(), b"same content")

    def test_overlapping_folder_selection_and_changed_descendant(self):
        child = self.file("folder/deep/a.txt", b"old")
        folder = self.root / "folder"
        scan = st.scan_tree(self.root)
        self.assertEqual(st.prepare_delete(scan, [folder, child]), [folder])
        self.file("folder/deep/new.txt", b"new")
        with self.assertRaises(st.StorageError):
            st.delete_selected(scan, [folder], recycler=self.recycle)
        self.assertTrue(child.exists())

    def test_folder_recycle_and_partial_cancellation_report(self):
        self.file("folder/a.txt", b"one")
        other = self.file("other.txt", b"two")
        scan = st.scan_tree(self.root)
        folder = self.root / "folder"
        def partial(paths):
            self.recycle(paths[:1])
            return "模拟系统取消剩余项"
        result = st.delete_selected(scan, [folder, other], recycler=partial)
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(len(result.remaining), 1)
        self.assertIn("取消", result.message)

    @unittest.skipUnless(os.name == "nt", "Windows Shell 文件操作结构")
    def test_native_request_has_absolute_double_null_paths_and_prompts(self):
        a, b = self.file("a & ! %.txt", b"a"), self.file("中文 b.txt", b"b")
        operation, buffer = st._shell_request([a, b])
        data = ctypes.wstring_at(ctypes.addressof(buffer), len(buffer))
        self.assertIn(str(a) + "\0" + str(b) + "\0\0", data)
        self.assertEqual(operation.wFunc, 3)
        self.assertTrue(operation.fFlags & 0x0040)
        self.assertTrue(operation.fFlags & 0x4000)
        self.assertFalse(operation.fFlags & 0x0010, "必须保留 Windows 系统确认")
        with self.assertRaises(st.StorageError):
            st._shell_request([self.root / "*.txt"])


if __name__ == "__main__":
    make_console_tolerant()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StorageTests))
    if result.wasSuccessful():
        print("空间分析与重复文件自检全部通过 ✔")
    raise SystemExit(0 if result.wasSuccessful() else 1)
