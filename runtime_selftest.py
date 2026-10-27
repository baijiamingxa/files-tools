#!/usr/bin/env python3
"""运行环境的离线回归；加 --real 可在临时目录验证真实下载与引擎。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import archive_runtime as native
import runtime_manager as runtime
import dependency_check as dependencies
import swift_extract as se
from txt_encoder_core import make_console_tolerant


class Response(io.BytesIO):
    def __init__(self, data, length=None, url="https://example.test/file"):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}
        self.url = url

    def geturl(self):
        return self.url


class RuntimeTests(unittest.TestCase):
    def test_dependency_missing_and_transitive_failure_are_distinct(self):
        def importer(name):
            if name == "pyzipper":
                raise ModuleNotFoundError("No module named Crypto", name="Crypto")
            if name == "py7zr":
                raise OSError("DLL load failed")
            raise ModuleNotFoundError(f"No module named {name}", name=name)
        with patch.object(runtime, "download_verified", side_effect=AssertionError("自检不能下载")):
            rows = {row.name: row for row in dependencies.inspect_dependencies(importer=importer)}
        self.assertEqual(rows["py7zr"].status, "加载失败")
        self.assertIn("DLL load failed", rows["py7zr"].detail)
        self.assertEqual(rows["pyzipper"].status, "加载失败")
        self.assertIn("Crypto", rows["pyzipper"].detail)
        self.assertEqual(rows["zstandard"].status, "未安装（可选）")
        self.assertIn("便携", rows["zstandard"].detail)

    def test_dependency_import_does_not_hide_operational_failure(self):
        def importer(name):
            if name == "zstandard":
                return SimpleNamespace(__version__="test-1", __file__="test/zstandard.py",
                    ZstdCompressor=lambda: SimpleNamespace(compress=lambda data: b"packed"),
                    ZstdDecompressor=lambda: SimpleNamespace(decompress=lambda data: b"bad contents"))
            raise ModuleNotFoundError(name, name=name)
        rows = {row.name: row for row in dependencies.inspect_dependencies(importer=importer)}
        self.assertEqual(rows["zstandard"].status, "自检失败")
        self.assertEqual(rows["zstandard"].version, "test-1")
        self.assertEqual(rows["zstandard"].location, "test/zstandard.py")
        self.assertIn("内容不一致", rows["zstandard"].detail)

    def test_dependency_roundtrip_version_and_cancellation(self):
        def importer(name):
            if name == "zstandard":
                return SimpleNamespace(__version__="test-2", __file__="local/zstandard.py",
                    ZstdCompressor=lambda: SimpleNamespace(compress=lambda data: data[::-1]),
                    ZstdDecompressor=lambda: SimpleNamespace(decompress=lambda data: data[::-1]))
            raise ModuleNotFoundError(name, name=name)
        rows = {row.name: row for row in dependencies.inspect_dependencies(importer=importer)}
        self.assertEqual(rows["zstandard"].status, "可用")
        self.assertIn("test-2", rows["zstandard"].report())
        self.assertEqual(dependencies.python_result().status, "可用")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(runtime.RuntimeCancelled):
            dependencies.inspect_dependencies(stop, importer=importer)

    def test_download_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "engine.exe"
            data = b"verified engine payload"
            expected = hashlib.sha256(data).hexdigest()
            runtime.download_verified(["https://example.test/file"], target, expected, opener=lambda *a, **k: Response(data))
            self.assertEqual(target.read_bytes(), data)

    def test_bad_hash_and_partial_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "engine.exe"
            for reply in (Response(b"bad"), Response(b"bad", length=90), Response(b"bad", url="http://example.test/file")):
                with self.subTest(reply=reply.headers), self.assertRaises(runtime.RuntimeErrorBase):
                    runtime.download_verified(["https://example.test/file"], target, "0" * 64, opener=lambda *a, **k: reply)
                self.assertFalse(target.exists())
            with self.assertRaises(runtime.RuntimeErrorBase):
                runtime.download_verified(["http://example.test/file"], target, "0" * 64)

    def test_mirror_and_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "engine.exe"
            data = b"good"
            with patch.object(runtime.urllib.request, "urlopen", side_effect=[OSError("offline"), Response(data)]) as opener:
                runtime.download_verified(["https://one.test/x", "https://two.test/x"], target, hashlib.sha256(data).hexdigest())
                self.assertEqual(opener.call_count, 2)
            stop = threading.Event()
            class CancelReply(Response):
                def read(self, count):
                    stop.set()
                    return super().read(count)
            with self.assertRaises(runtime.RuntimeCancelled):
                runtime.download_verified(["https://example.test/x"], target, hashlib.sha256(data).hexdigest(), stop, opener=lambda *a, **k: CancelReply(data))
            self.assertFalse(target.exists())

    def test_subprocess_output_cancel_and_timeout(self):
        code, output = runtime.run_process([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"])
        self.assertEqual(code, 0)
        self.assertIn("out", output)
        self.assertIn("err", output)
        with self.assertRaises(runtime.RuntimeErrorBase):
            runtime.run_process([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.1)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(runtime.RuntimeCancelled):
            runtime.run_process([sys.executable, "-c", "raise SystemExit(99)"], stop)

    def test_architecture(self):
        with patch.dict(os.environ, {"PROCESSOR_ARCHITEW6432": ""}):
            for machine, expected in (("AMD64", "x64"), ("ARM64", "arm64"), ("i686", "x86")):
                with patch.object(runtime.platform, "machine", return_value=machine):
                    self.assertEqual(runtime.architecture(), expected)

    def test_manifest_damage(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runtime, "architecture", return_value="x64"):
            directory = runtime.tool_directory("7zip", temp)
            directory.mkdir()
            for name in ("7z.exe", "7z.dll"):
                (directory / name).write_bytes(name.encode())
            manifest = {name: runtime.digest_file(directory / name) for name in ("7z.exe", "7z.dll")}
            (directory / "verified.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(runtime, "probe", return_value=True):
                self.assertEqual(runtime.installed_tool("7zip", temp), directory / "7z.exe")
                (directory / "7z.dll").write_bytes(b"damaged")
                self.assertIsNone(runtime.installed_tool("7zip", temp))
            (directory / "verified.json").write_text("[]", encoding="utf-8")
            self.assertIsNone(runtime.installed_tool("7zip", temp))

    def test_install_atomic_failure_and_success(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runtime, "architecture", return_value="x64"):
            root = Path(temp)
            destination = runtime.tool_directory("7zip", root)
            destination.mkdir()
            (destination / "previous.txt").write_text("old", encoding="utf8")
            calls = []
            def download(urls, target, digest, stop, progress):
                calls.append((urls, digest))
                target.write_bytes(b"fixture")
            def unpack(command, stop):
                stage = Path(next(arg[2:] for arg in command if str(arg).startswith("-o")))
                (stage / "7z.exe").write_bytes(b"new exe")
                (stage / "7z.dll").write_bytes(b"new dll")
                return 0, "ok"
            with patch.object(runtime, "run_process", side_effect=unpack), patch.object(runtime, "probe", return_value=False):
                with self.assertRaises(runtime.RuntimeErrorBase):
                    runtime._install_locked("7zip", root, None, None, download)
            self.assertEqual((destination / "previous.txt").read_text(), "old")
            with patch.object(runtime, "run_process", side_effect=unpack), patch.object(runtime, "probe", return_value=True):
                executable = runtime._install_locked("7zip", root, None, None, download)
                self.assertEqual(executable.read_bytes(), b"new exe")
                self.assertEqual(runtime.installed_tool("7zip", root), executable)
            self.assertEqual(len(calls), 4)
            self.assertEqual(sorted(path.name for path in root.iterdir()), [destination.name])

    def test_zip_extraction_flattens_only_expected_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "package.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("bundle/zstd.exe", b"engine")
                archive.writestr("../../escape.txt", b"ignore")
                archive.writestr("bundle/dll/other.dll", b"ignore")
            stage = root / "stage"
            stage.mkdir()
            runtime._extract_zstd(package, stage)
            self.assertEqual([p.name for p in stage.iterdir()], ["zstd.exe"])
            self.assertFalse((root / "escape.txt").exists())

    def test_archive_paths_and_links(self):
        native.validate_listing("Path = 中文目录/a.txt\nSize = 3\nAttributes = A\n")
        for name in ("../escape", "C:/escape", "/escape", "//server/share", "safe:stream", "a/../../escape"):
            with self.subTest(name=name), self.assertRaises(se.EngineError):
                native.validate_listing("Path = " + name)
        for value in ("Symbolic Link = ../escape", "Hard Link = file", "Reparse = +", "Attributes = A lrwxrwxrwx"):
            with self.assertRaises(se.EngineError):
                native.validate_listing(value)

    def test_merge_preserves_old_on_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / "stage"
            stage.mkdir()
            (stage / "a.txt").write_bytes(b"new")
            out = root / "out"
            out.mkdir()
            (out / "a.txt").write_bytes(b"old")
            job = se.Job("unused", cancel=True)
            with self.assertRaises(se.Cancelled):
                native._merge(stage, out, job)
            self.assertEqual((out / "a.txt").read_bytes(), b"old")
            job.cancel = False
            native._merge(stage, out, job)
            self.assertEqual((out / "a.txt").read_bytes(), b"new")

    def test_archive_routing_without_optional_packages(self):
        with patch.multiple(se, py7zr=None, pyzipper=None, zstandard=None, WINRAR=None, UNRAR=None), patch.object(runtime, "get_tool", return_value=Path("engine")):
            with patch.object(native, "extract") as extract:
                for kind in ("7z", "rar", "zst", "cab"):
                    se.dispatch_engine(se.Job("unused", kind=kind), se.Options())
                self.assertEqual(extract.call_count, 4)
            with patch.object(native, "compress") as compress:
                for kind in ("zip", "7z"):
                    se.run_compress(se.Job("unused", kind=kind, password="secret"), se.Options())
                self.assertEqual(compress.call_count, 2)
            with patch.object(native, "compress_tar_zstd") as compress:
                se.run_compress(se.Job("unused", kind="tar.zst"), se.Options())
                compress.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows 的便携组件自动配置")
    def test_ready_does_not_download_and_failures_reported(self):
        with patch.object(runtime, "installed_tool", return_value=Path("ready.exe")), patch.object(runtime, "install_tool") as install, patch.dict(runtime._tools, {}, clear=True):
            results = runtime.configure()
            self.assertTrue(all(row[1] == "可用" for row in results))
            install.assert_not_called()
        with patch.object(runtime, "installed_tool", return_value=None), patch.object(runtime, "existing_system_tool", return_value=None), patch.object(runtime, "supplied_by_existing_libraries", return_value=False), patch.object(runtime, "install_tool", side_effect=OSError("offline")), patch.dict(runtime._tools, {}, clear=True):
            results = runtime.configure()
            self.assertEqual([row[1] for row in results], ["配置失败", "配置失败"])
            self.assertEqual(runtime._tools, {})

    def test_existing_libraries_avoid_unnecessary_downloads(self):
        with patch.multiple(se, py7zr=True, pyzipper=True, zstandard=True, WINRAR="existing", UNRAR=None):
            self.assertTrue(runtime.supplied_by_existing_libraries("7zip"))
            self.assertTrue(runtime.supplied_by_existing_libraries("zstd"))
        with patch.multiple(se, py7zr=None, pyzipper=None, zstandard=None, WINRAR=None, UNRAR=None):
            self.assertFalse(runtime.supplied_by_existing_libraries("7zip"))
            self.assertFalse(runtime.supplied_by_existing_libraries("zstd"))


def real_test():
    if os.name != "nt":
        raise RuntimeError("真实下载测试需要 Windows。")
    with tempfile.TemporaryDirectory(prefix="toolbox-runtime-real-") as temp, patch.dict(runtime._tools, {}, clear=True), patch.multiple(se, py7zr=None, pyzipper=None, zstandard=None, WINRAR=None, UNRAR=None), patch.object(runtime, "existing_system_tool", return_value=None):
        root = Path(temp)
        rows = runtime.configure(root=root / "runtime", progress=lambda value: print(value, flush=True))
        assert all(row[1] == "可用" for row in rows), rows
        with patch.object(runtime, "download_verified", side_effect=AssertionError("就绪后不应联网")):
            assert all(row[1] == "可用" for row in runtime.configure(root=root / "runtime"))
        source = root / "中文资料"
        source.mkdir()
        (source / "空目录").mkdir()
        sample = "真实便携组件往返验证。\n" * 100
        (source / "文稿.txt").write_text(sample, encoding="utf-8")
        for kind, password in (("7z", ""), ("7z", "密码&%123"), ("zip", "ascii&%123"), ("tar.zst", "")):
            archive = root / ("测试" + ("加密" if password else "") + "." + kind)
            job = se.Job(str(archive), kind=kind, mode="compress", sources=[str(source)], password=password)
            se.run_compress(job, se.Options())
            out = root / (archive.name + "-展开")
            extracted = se.Job(str(archive), kind=se.detect_format(str(archive)), dest=str(out), password=password)
            se.dispatch_engine(extracted, se.Options())
            assert (out / source.name / "文稿.txt").read_text(encoding="utf-8") == sample
            assert (out / source.name / "空目录").is_dir()
            if password:
                wrong = se.Job(str(archive), kind=extracted.kind, dest=str(root / "错误密码输出"), password="wrong")
                try:
                    se.dispatch_engine(wrong, se.Options())
                except se.NeedPassword:
                    pass
                else:
                    raise AssertionError("错误密码未被识别")
                assert not (root / "错误密码输出").exists()
            print(f"[通过] 真实 {kind} {'加密' if password else ''}压缩解压、中文路径、空目录、错误密码", flush=True)
    print("真实下载与便携引擎自检全部通过 ✔")


if __name__ == "__main__":
    make_console_tolerant()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeTests))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("运行环境离线自检全部通过 ✔")
    if "--real" in sys.argv:
        real_test()
