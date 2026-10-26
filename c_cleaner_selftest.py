#!/usr/bin/env python3
"""C 盘清理核心自检：扫描统计、年龄保护、占用跳过、联接点防护、注册表与管理员任务管道。

清理动作全部通过注入的临时目录执行，不碰真实系统路径；
注册表测试使用 HKCU 下独立的临时键。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import c_cleaner as cc


class ScanCleanTests(unittest.TestCase):
    def test_age_filter_scan_and_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old = base / "旧临时.bin"
            old.write_bytes(b"a" * 3333)
            fresh = base / "新临时.bin"
            fresh.write_bytes(b"b" * 111)
            past = time.time() - 3 * 24 * 3600
            os.utime(old, (past, past))
            engine = cc.CleanEngine(root_overrides={"user_temp": base}, min_age_hours=24)
            scan = engine.scan("user_temp")
            self.assertEqual(scan.bytes, 3333)
            self.assertEqual(scan.files, 1)
            result = engine.clean("user_temp")
            self.assertTrue(result.ok)
            self.assertEqual(result.bytes_freed, 3333)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists(), "24 小时内的文件不能被清理")
            # 关闭年龄保护后，剩下的也会被清掉
            engine0 = cc.CleanEngine(root_overrides={"user_temp": base}, min_age_hours=0)
            result0 = engine0.clean("user_temp")
            self.assertEqual(result0.bytes_freed, 111)
            self.assertFalse(fresh.exists())

    def test_scan_skips_missing_root(self):
        engine = cc.CleanEngine(root_overrides={"user_temp": "不存在的目录"}, min_age_hours=0)
        scan = engine.scan("user_temp")
        self.assertEqual(scan.bytes, 0)

    @unittest.skipUnless(os.name == "nt", "文件占用锁定只在 Windows 上成立")
    def test_locked_file_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            locked = base / "locked.tmp"
            locked.write_bytes(b"z" * 512)
            engine = cc.CleanEngine(root_overrides={"user_temp": base}, min_age_hours=0)
            with open(locked, "rb"):  # 保持句柄，Windows 下删除会失败
                result = engine.clean("user_temp")
            self.assertTrue(locked.exists())
            self.assertGreaterEqual(result.skipped, 1)
            self.assertIn("占用", result.note)

    @unittest.skipUnless(os.name == "nt", "需要 mklink 创建目录联接")
    def test_junction_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            outside = base / "outside"
            outside.mkdir()
            (outside / "deep.bin").write_bytes(b"q" * 9999)
            inside = base / "inside"
            inside.mkdir()
            (inside / "shallow.bin").write_bytes(b"w" * 10)
            subprocess.run(["cmd", "/c", "mklink", "/J", str(inside / "link"), str(outside)],
                           check=True, capture_output=True)
            scan = cc.CleanEngine(root_overrides={"user_temp": inside}, min_age_hours=0).scan("user_temp")
            self.assertEqual(scan.bytes, 10, "联接点指向的目录不能被进入")

    def test_thumbnails_pattern(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cache = base / "thumbcache_001.db"
            cache.write_bytes(b"t" * 256)
            other = base / "notcache.db"
            other.write_bytes(b"o" * 999)
            engine = cc.CleanEngine(root_overrides={"thumbnails": base}, min_age_hours=0)
            scan = engine.scan("thumbnails")
            self.assertEqual(scan.bytes, 256)
            result = engine.clean("thumbnails")
            self.assertFalse(cache.exists())
            self.assertTrue(other.exists())
            self.assertEqual(result.bytes_freed, 256)

    def test_defs_consistent_and_scan_never_raises(self):
        defs = cc.build_items()
        ids = [d.id for d in defs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(cc.ADMIN_ITEM_IDS <= set(ids))
        engine = cc.CleanEngine()
        for d in defs:
            scan = engine.scan(d.id)  # 只读扫描；在两个平台上都不允许抛异常
            self.assertIsInstance(scan, cc.ItemScan)
            self.assertGreaterEqual(scan.bytes, 0)

    @unittest.skipUnless(os.name == "nt", "回收站 API 仅 Windows")
    def test_recycle_query_readonly(self):
        size, items = cc.recycle_bin_info("C:\\")
        self.assertGreaterEqual(size, 0)
        self.assertGreaterEqual(items, 0)


class RegistryTests(unittest.TestCase):
    SCRATCH = r"Software\TxtEncoderCleanerSelfTest"

    def setUp(self):
        cc.registry_delete_tree(self.SCRATCH)

    def tearDown(self):
        cc.registry_delete_tree(self.SCRATCH)

    @unittest.skipUnless(os.name == "nt", "注册表仅 Windows")
    def test_recent_docs_registry_and_files(self):
        winreg = cc._reg()
        # 覆盖的是 Explorer 的“基键”，实际清理它下面的 RecentDocs 等四个子键
        scratch = self.SCRATCH
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scratch + r"\RecentDocs",
                                0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "讲稿.lnk", 0, winreg.REG_SZ, "x")
            sub = winreg.CreateKeyEx(key, "ext0", 0, winreg.KEY_WRITE)
            winreg.SetValueEx(sub, "v", 0, winreg.REG_SZ, "y")
            sub.Close()
        with tempfile.TemporaryDirectory() as temp:
            recent = Path(temp)
            (recent / "讲稿.lnk").write_bytes(b"l" * 64)
            engine = cc.CleanEngine(
                root_overrides={"recent_docs": recent},
                registry_overrides={"recent_docs": scratch}, min_age_hours=0)
            scan = engine.scan("recent_docs")
            self.assertEqual(scan.files, 1)
            self.assertEqual(scan.records, 2)
            result = engine.clean("recent_docs")
            self.assertTrue(result.ok)
            self.assertEqual(result.records_cleared, 2)
            self.assertFalse((recent / "讲稿.lnk").exists())
            # 子键（RecentDocs）整个删除；基键保留但值已清空——生产中绝不能删 Explorer 基键
            with self.assertRaises(OSError):
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, scratch + r"\RecentDocs")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, scratch) as leftover:
                self.assertEqual(winreg.QueryInfoKey(leftover)[1], 0)


class AdminTaskTests(unittest.TestCase):
    def test_task_runner_filters_unknown_and_non_admin_items(self):
        outcome = cc.run_admin_task({"items": ["user_temp", "recycle_bin", "不存在"]})
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["results"], [], "管理员任务只执行 ADMIN_ITEM_IDS 里的项目")

    def test_entry_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / f"task_{uuid.uuid4().hex}.json"
            result = Path(str(task) + ".result.json")
            task.write_text(json.dumps({"items": ["不存在"]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(cc.run_admin_task_entry([cc.ADMIN_TASK_FLAG, str(task)]), 0)
            outcome = json.loads(result.read_text(encoding="utf-8"))
            self.assertTrue(outcome["ok"])
            self.assertEqual(outcome["results"], [])
            task.unlink()
            result.unlink()

    def test_entry_reports_unreadable_task(self):
        with tempfile.TemporaryDirectory() as temp:
            task = Path(temp) / "missing.json"
            result = Path(str(task) + ".result.json")
            self.assertEqual(cc.run_admin_task_entry([cc.ADMIN_TASK_FLAG, str(task)]), 0)
            outcome = json.loads(result.read_text(encoding="utf-8"))
            self.assertFalse(outcome["ok"])
            self.assertIn("任务文件", outcome["error"])

    def test_merge_admin_outcome(self):
        good = {"ok": True, "results": [{"item": "dns_cache", "note": "已刷新"}]}
        merged = cc.merge_admin_outcome(good, ["dns_cache", "prefetch"])
        self.assertTrue(merged["dns_cache"].ok)
        self.assertEqual(merged["dns_cache"].note, "已刷新")
        self.assertFalse(merged["prefetch"].ok)
        self.assertIn("没有返回", merged["prefetch"].error)
        timeout = cc.merge_admin_outcome(None, ["dns_cache"])
        self.assertFalse(timeout["dns_cache"].ok)
        failed = cc.merge_admin_outcome({"ok": False, "error": "取消了授权"}, ["dns_cache"])
        self.assertIn("取消", failed["dns_cache"].error)

    def test_wait_for_result_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            result = Path(temp) / "never.json"
            notes = []
            outcome = cc.wait_for_result(result, timeout_seconds=0.1, poll=0.05,
                                         progress=notes.append)
            self.assertIsNone(outcome)
            self.assertTrue(any("管理员任务" in n for n in notes))

    def test_elevated_command_shape(self):
        # 不真的发起 UAC；只验证提权命令把脚本/任务文件参数拼对了。
        launcher, args = cc._elevated_command(Path("C:/任务 文件.json"))
        self.assertIn(cc.ADMIN_TASK_FLAG, args)
        self.assertIn("任务 文件.json", args)
        if getattr(sys, "frozen", False):
            self.assertEqual(Path(launcher), Path(sys.executable))
        else:
            self.assertTrue(Path(launcher).is_file())
            self.assertIn(str(Path(cc.__file__).resolve()), args)


if __name__ == "__main__":
    from txt_encoder_core import make_console_tolerant
    make_console_tolerant()
    result = unittest.main(exit=False, verbosity=2).result
    if result.wasSuccessful():
        print("C 盘清理自检全部通过 ✔")
        raise SystemExit(0)
    raise SystemExit(1)
