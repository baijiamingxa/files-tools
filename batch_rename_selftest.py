#!/usr/bin/env python3
"""用临时文件验证改名、撤销、冲突拒绝和中断回退，不碰真实用户文件。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import batch_rename as br
from txt_encoder_core import make_console_tolerant


class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "中文 文件 & ! %"
        self.data.mkdir()
        self.records = self.root / "records"

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, content=b"original"):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_preview_combined_rules_and_undo(self):
        b = self.file("旧乙.txt", b"second")
        a = self.file("旧甲.txt", b"first")
        files = br.collect_files([self.data, a], extensions="txt")
        before = {p.name: p.read_bytes() for p in files}
        options = br.RenameOptions(find="旧", replacement="新", prefix="前_", suffix="_后", numbering=True, start=7, step=2, width=3, extension="md")
        rows = br.build_plan(files, options)
        self.assertTrue(all(not row.error for row in rows))
        self.assertEqual({row.dst.name for row in rows}, {f"前_新{row.src.stem[1:]}_{7+i*2:03d}_后.md" for i, row in enumerate(rows)})
        self.assertEqual(before, {p.name: p.read_bytes() for p in files})
        result = br.execute_plan(rows, self.records)
        self.assertEqual(json.loads(result.record_path.read_text(encoding="utf-8"))["status"], "completed")
        for row in rows:
            self.assertEqual(row.dst.read_bytes(), before[row.src.name])
            self.assertFalse(row.src.exists())
        br.execute_plan(result.undo_rows, self.records)
        self.assertEqual(before, {p.name: p.read_bytes() for p in files})

    def test_dates_and_number_prefix(self):
        src = self.file("日期.log")
        timestamp = datetime(2020, 6, 7, 8, 9, 10).timestamp()
        os.utime(src, (timestamp, timestamp))
        row = br.build_plan([src], br.RenameOptions(mode="date", numbering=True, number_position="prefix", start=2, width=2))[0]
        self.assertEqual(row.dst.name, "02_20200607_080910.log")
        self.assertFalse(row.error)

    def test_fixed_name_collision_and_case_conflict(self):
        a, b = self.file("a.txt"), self.file("b.txt")
        rows = br.build_plan([a, b], br.RenameOptions(mode="fixed", base_name="相同"))
        self.assertTrue(all(row.error for row in rows))
        with self.assertRaises(br.RenameError):
            br.execute_plan(rows, self.records)
        self.assertTrue(a.exists() and b.exists())
        self.file("TAKEN.TXT", b"keep")
        row = br.build_plan([a], br.RenameOptions(mode="fixed", base_name="taken"))[0]
        self.assertIn("占用", row.error)

    def test_invalid_names_and_directory_collision(self):
        source = self.file("test.txt")
        for name in ("CON", "COM1", "../escape", "a:b", "a?b", "x" * 256):
            with self.subTest(name=name):
                row = br.build_plan([source], br.RenameOptions(mode="fixed", base_name=name))[0]
                self.assertTrue(row.error)
        (self.data / "folder.txt").mkdir()
        self.assertTrue(br.build_plan([source], br.RenameOptions(mode="fixed", base_name="folder"))[0].error)
        self.assertTrue(br.invalid_name("bad. "))

    def test_preview_stale_source_and_destination(self):
        source = self.file("a.txt", b"old")
        rows = br.build_plan([source], br.RenameOptions(prefix="new_"))
        source.write_bytes(b"modified after preview")
        with self.assertRaises(br.RenameError):
            br.execute_plan(rows, self.records)
        self.assertEqual(source.read_bytes(), b"modified after preview")
        rows = br.build_plan([source], br.RenameOptions(prefix="new_"))
        target = self.file("new_a.txt", b"external")
        with self.assertRaises(br.RenameError):
            br.execute_plan(rows, self.records)
        self.assertEqual(target.read_bytes(), b"external")

    def test_swap_cycle_and_case_only(self):
        a, b = self.file("a.txt", b"A"), self.file("b.txt", b"B")
        rows = [br.RenameRow(a, b, br.file_signature(a)), br.RenameRow(b, a, br.file_signature(b))]
        result = br.execute_plan(rows, self.records)
        self.assertEqual(a.read_bytes(), b"B")
        self.assertEqual(b.read_bytes(), b"A")
        br.execute_plan(result.undo_rows, self.records)
        rows = br.build_plan([a], br.RenameOptions(mode="fixed", base_name="A"))
        result = br.execute_plan(rows, self.records)
        self.assertIn("A.txt", [p.name for p in self.data.iterdir()])
        br.execute_plan(result.undo_rows, self.records)
        self.assertIn("a.txt", [p.name for p in self.data.iterdir()])

    def test_failure_after_partial_commit_rolls_back(self):
        a, b = self.file("a.txt", b"A"), self.file("b.txt", b"B")
        rows = br.build_plan([a, b], br.RenameOptions(prefix="new_"))
        move = br._move_no_replace
        def fail(source, target):
            if target == self.data / "new_b.txt":
                raise PermissionError("模拟文件占用")
            move(source, target)
        with patch.object(br, "_move_no_replace", fail):
            with self.assertRaises(br.RenameError):
                br.execute_plan(rows, self.records)
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, {"a.txt": b"A", "b.txt": b"B"})
        record = json.loads(next(self.records.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "rolled_back")

    def test_target_created_during_execution_is_preserved(self):
        source = self.file("a.txt", b"A")
        target = self.data / "new_a.txt"
        rows = br.build_plan([source], br.RenameOptions(prefix="new_"))
        move = br._move_no_replace
        def race(old, new):
            if new == target:
                target.write_bytes(b"created by another app")
            move(old, new)
        with patch.object(br, "_move_no_replace", race):
            with self.assertRaises(br.RenameError):
                br.execute_plan(rows, self.records)
        self.assertEqual(source.read_bytes(), b"A")
        self.assertEqual(target.read_bytes(), b"created by another app")

    def test_rollback_keeps_recovery_file_if_original_name_is_taken(self):
        a, b = self.file("a.txt", b"A"), self.file("b.txt", b"B")
        rows = br.build_plan([a, b], br.RenameOptions(prefix="new_"))
        move = br._move_no_replace
        def race(source, target):
            if target == self.data / "new_b.txt":
                a.write_bytes(b"external file")
                raise PermissionError("模拟第二个目标被占用")
            move(source, target)
        with patch.object(br, "_move_no_replace", race):
            with self.assertRaisesRegex(br.RenameError, "恢复未全部完成"):
                br.execute_plan(rows, self.records)
        self.assertEqual(a.read_bytes(), b"external file")
        self.assertEqual(b.read_bytes(), b"B")
        record = json.loads(next(self.records.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "recovery_needed")
        self.assertEqual(Path(record["locations"][0]).read_bytes(), b"A")

    def test_cancel_after_first_commit_and_undo_rejects_modified_file(self):
        a, b = self.file("a.txt", b"A"), self.file("b.txt", b"B")
        rows = br.build_plan([a, b], br.RenameOptions(prefix="new_"))
        stop = threading.Event()
        with self.assertRaises(br.RenameError):
            br.execute_plan(rows, self.records, stop, lambda *_: stop.set())
        self.assertEqual(a.read_bytes(), b"A")
        self.assertEqual(b.read_bytes(), b"B")
        result = br.execute_plan(rows, self.records)
        result.undo_rows[0].src.write_bytes(b"changed after rename")
        with self.assertRaises(br.RenameError):
            br.execute_plan(result.undo_rows, self.records)
        self.assertFalse(a.exists())

    def test_recursive_filters_and_no_change(self):
        a = self.file("a.TXT")
        nested = self.file("sub/b.txt")
        self.file("other.bin")
        self.assertEqual(br.collect_files([self.data], extensions="txt"), [a])
        self.assertEqual(set(br.collect_files([self.data], True, "txt")), {a, nested})
        result = br.execute_plan(br.build_plan([a], br.RenameOptions()), self.records)
        self.assertIsNone(result.record_path)
        self.assertTrue(a.exists())


if __name__ == "__main__":
    make_console_tolerant()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RenameTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("批量重命名自检全部通过 ✔")
    raise SystemExit(0 if result.wasSuccessful() else 1)
