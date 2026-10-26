#!/usr/bin/env python3
"""工具箱集成自检；只访问本地 HTTP 服务，不依赖网络和扩展库。"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import re
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import direct_downloader as dl
import swift_extract as se
from txt_encoder_core import ConvertOptions, convert_file, make_console_tolerant


@contextmanager
def local_server(data, slow_delay=0.003):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.serve(True)

        def do_GET(self):
            self.serve(False)

        def serve(self, head):
            ranged = self.path not in ("/stream", "/no-range", "/fake-range")
            unknown = self.path == "/stream"
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "")) if ranged else None
            start = int(match[1]) if match else 0
            end = min(int(match[2]), len(data) - 1) if match and match[2] else len(data) - 1
            self.send_response(206 if match else 200)
            if not unknown:
                self.send_header("Content-Length", str(end - start + 1))
            if ranged or self.path == "/fake-range":
                self.send_header("Accept-Ranges", "bytes")
            if match:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            if self.path == "/unsafe":
                self.send_header("Content-Disposition", 'attachment; filename="../../outside.txt"')
            self.end_headers()
            if head:
                return
            try:
                for offset in range(start, end + 1, 65536):
                    self.wfile.write(data[offset:min(offset + 65536, end + 1)])
                    if self.path == "/slow":
                        time.sleep(slow_delay)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ToolboxTests(unittest.TestCase):
    def test_split_zip_selection_uses_zip_index(self):
        with tempfile.TemporaryDirectory() as temp:
            index = Path(temp) / "分卷压缩.zip"
            first_part = index.with_suffix(".z01")
            with zipfile.ZipFile(index, "w") as zf:
                zf.writestr("test.txt", b"volume test")
            first_part.write_bytes(b"first volume")
            canonical, group = se.volume_base(str(first_part))
            self.assertEqual(canonical, str(index))
            self.assertEqual(se.all_volume_parts(canonical, group), [str(first_part), str(index)])

    def test_scheduler_accepts_tasks_while_another_runs(self):
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        scheduler = se.Scheduler(se.Options(parallel_jobs=2))
        first = se.Job(path="slow")
        second = se.Job(path="next")
        def execute(job, _opts):
            if job is first:
                started.set()
                release.wait(5)
            else:
                completed.set()
            job.state = "完成"
        with patch.object(se, "execute_task", execute):
            try:
                scheduler.start([first])
                self.assertTrue(started.wait(3))
                # 一个空闲线程退出后，第一个任务仍在进行。
                time.sleep(0.7)
                scheduler.start([second])
                self.assertTrue(completed.wait(3))
                self.assertTrue(any(t.is_alive() for t in scheduler.workers))
            finally:
                release.set()
                for thread in list(scheduler.workers):
                    thread.join(5)
        self.assertEqual(first.state, "完成")
        self.assertEqual(second.state, "完成")

    def test_download_modes(self):
        data = bytes(range(256)) * 8193
        with tempfile.TemporaryDirectory() as temp, local_server(data) as base:
            for route in ("range", "no-range", "stream", "fake-range"):
                with self.subTest(route=route):
                    path = Path(temp) / (route + ".bin")
                    job = dl.DownloadJob(base + "/" + route, str(path), threads=4)
                    job.run()
                    self.assertIsNone(job.error)
                    self.assertTrue(job.finished)
                    self.assertEqual(path.read_bytes(), data)
                    self.assertFalse(Path(job.part).exists())
                    self.assertFalse(Path(job.sidecar).exists())

    def test_cancel_resume(self):
        data = bytes(range(251)) * 17000
        with tempfile.TemporaryDirectory() as temp, local_server(data) as base:
            path = Path(temp) / "resume.bin"
            job = dl.DownloadJob(base + "/slow", str(path), threads=4)
            def status(done, total, msg=None):
                if done and msg is None:
                    job.cancel_event.set()
            job.status_cb = status
            job.run()
            self.assertFalse(job.finished)
            self.assertFalse(path.exists())
            self.assertTrue(Path(job.sidecar).exists())
            self.assertGreater(Path(job.part).stat().st_size, 0)
            resumed = dl.DownloadJob(base + "/slow", str(path), threads=4)
            resumed.run()
            self.assertIsNone(resumed.error)
            self.assertTrue(resumed.finished)
            self.assertEqual(path.read_bytes(), data)

    def test_server_filename_stays_in_output(self):
        with tempfile.TemporaryDirectory() as temp, local_server(b"hello") as base:
            job = dl.DownloadJob(base + "/unsafe", None, save_dir=temp)
            job.run()
            self.assertTrue(job.finished)
            self.assertEqual(Path(job.save_path).parent, Path(temp))
            self.assertEqual(Path(job.save_path).name, "outside.txt")
        self.assertEqual(dl.guess_filename("https://example.invalid/file", "attachment; filename*=UTF-8''..%2F..%2Fx.txt"), "x.txt")
        self.assertEqual(dl.safe_filename("CON.txt"), "_CON.txt")

    def test_download_extract_convert(self):
        text = "下载、解压、转码的完整流程。\r\n第二行。\r\n"
        raw = text.encode("gbk")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("中文目录/文稿.txt", raw)
        with tempfile.TemporaryDirectory() as temp, local_server(archive.getvalue()) as base:
            root = Path(temp)
            downloaded = root / "bundle.zip"
            download = dl.DownloadJob(base + "/range", str(downloaded))
            download.run()
            self.assertTrue(download.finished)
            dest = root / "extracted"
            job = se.Job(path=str(downloaded), kind=se.detect_format(str(downloaded)), dest=str(dest))
            se.execute_task(job, se.Options(workers=2))
            self.assertEqual(job.state, "完成", job.error)
            source = dest / "中文目录/文稿.txt"
            result = convert_file(source, ConvertOptions(target_encoding="utf-8", output_dir=root / "utf8"), dest)
            self.assertEqual(result.status, "converted", result.error)
            self.assertEqual(result.dst.read_bytes().decode("utf-8"), text)
            self.assertEqual(source.read_bytes(), raw)

    def test_compress_extract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "资料"
            source.mkdir()
            (source / "a.txt").write_bytes(b"content\n" * 100)
            (source / "empty.txt").touch()
            for kind in ("zip", "tar", "tar.gz", "tar.xz"):
                with self.subTest(kind=kind):
                    archive = root / ("bundle." + kind)
                    opts = se.Options(workers=2)
                    job = se.Job(path=str(archive), mode="compress", kind=kind, sources=[str(source)], dest=str(root))
                    se.execute_task(job, opts)
                    self.assertEqual(job.state, "完成", job.error)
                    dest = root / ("out_" + kind)
                    extracted = se.Job(path=str(archive), kind=se.detect_format(str(archive)), dest=str(dest))
                    se.execute_task(extracted, opts)
                    self.assertEqual(extracted.state, "完成", extracted.error)
                    files = sorted(p for p in dest.rglob("*") if p.is_file())
                    self.assertEqual([p.name for p in files], ["a.txt", "empty.txt"])
                    self.assertEqual(files[0].read_bytes(), (source / "a.txt").read_bytes())

    def test_single_file_streams(self):
        data = b"stream content\n" * 100
        with tempfile.TemporaryDirectory() as temp:
            for ext, compress in (("gz", gzip.compress), ("bz2", bz2.compress), ("xz", lzma.compress)):
                with self.subTest(ext=ext):
                    path = Path(temp) / ("sample.txt." + ext)
                    path.write_bytes(compress(data))
                    dest = Path(temp) / ext
                    job = se.Job(path=str(path), kind=se.detect_format(str(path)), dest=str(dest))
                    se.execute_task(job, se.Options())
                    self.assertEqual(job.state, "完成", job.error)
                    self.assertEqual((dest / "sample.txt").read_bytes(), data)

    def test_zip_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "unsafe.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("../../escape.txt", b"safe inside destination")
            dest = Path(temp) / "out"
            job = se.Job(path=str(path), kind="zip", dest=str(dest))
            se.execute_task(job, se.Options())
            self.assertEqual(job.state, "完成", job.error)
            self.assertFalse((Path(temp) / "escape.txt").exists())
            self.assertEqual((dest / "escape.txt").read_bytes(), b"safe inside destination")


if __name__ == "__main__":
    make_console_tolerant()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ToolboxTests))
    if result.wasSuccessful():
        print("工具箱集成自检全部通过 ✔")
    raise SystemExit(0 if result.wasSuccessful() else 1)
