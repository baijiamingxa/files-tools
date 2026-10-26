#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直链下载器 —— HTTP 多线程分段（分流）下载

用法:
  图形界面:   python direct_downloader.py
  命令行:     python direct_downloader.py URL [-n 线程数] [-o 保存路径] [-H 头名:值] ...

功能:
  - 用 HTTP Range 请求把文件切成 N 段，多线程并发下载
  - 服务器不支持分段时自动回落为单线程流式下载
  - 断点续传: 中途取消/断开后，重新下载同一 URL 会自动续传
  - 每个分段失败自动重试
  - 实时显示进度、速度、剩余时间
"""

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 256 * 1024          # 每次读写块大小
DEFAULT_THREADS = 8
MAX_RETRIES = 8             # 单分段重试次数
TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _align_up(x, m):
    return ((x + m - 1) // m) * m


def safe_filename(name):
    """服务器提供的文件名只作为名称使用，不能逃出保存目录。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        return "download.bin"
    if name.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }:
        name = "_" + name
    return name


def guess_filename(url, disposition=None):
    """优先 Content-Disposition，其次 URL 路径，最后兜底名。"""
    if disposition:
        m = re.search(r"filename\*\s*=\s*[\w-]+''([^;]+)", disposition, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"'))
            if name:
                return safe_filename(name)
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
        if m:
            name = m.group(1).strip()
            if name:
                return safe_filename(urllib.parse.unquote(name, encoding="utf-8", errors="replace"))
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(os.path.basename(path)).strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return safe_filename(name)


def unique_path(dirpath, filename):
    """文件已存在时自动加 (1)(2)... 后缀。"""
    base, ext = os.path.splitext(filename)
    cand = os.path.join(dirpath, filename)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(dirpath, f"{base}({i}){ext}")
        i += 1
    return cand


class ProbeResult:
    def __init__(self):
        self.total = None          # 文件总字节, None=未知
        self.ranged = False        # 服务器是否支持 Range 分段
        self.filename = None       # 建议文件名
        self.final_url = None


def _validate_range(url, headers, total):
    """抽样验证服务器的分段响应是否可靠(防"伪 Range"CDN)。"""
    mid = total // 2
    exp = min(4096, total - mid)
    h = dict(headers)
    h["Range"] = f"bytes={mid}-{mid + exp - 1}"
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 206:
                return False
            cr = r.headers.get("Content-Range") or ""
            if not cr.endswith(f"/{total}") and f"{mid}-" not in cr:
                return False
            data = r.read()
            return len(data) == exp
    except Exception:
        return False


def probe(url, headers):
    """探测文件大小、是否支持 Range、建议文件名。"""
    res = ProbeResult()
    # 先 HEAD
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            res.final_url = r.geturl()
            cl = r.headers.get("Content-Length")
            res.total = int(cl) if cl and cl.isdigit() else None
            if r.headers.get("Accept-Ranges", "").lower() == "bytes":
                res.ranged = True
            res.filename = guess_filename(url, r.headers.get("Content-Disposition"))
            if res.total is not None and res.ranged:
                return res
    except Exception:
        pass
    # HEAD 不可用时用 GET Range 0-0 探测
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            res.final_url = r.geturl()
            res.filename = res.filename or guess_filename(
                url, r.headers.get("Content-Disposition"))
            cr = r.headers.get("Content-Range")
            m = re.search(r"/(\d+)\s*$", cr or "")
            if r.status == 206 and m:
                res.total = int(m.group(1))
                res.ranged = True
            else:
                res.ranged = False
                if res.total is None:
                    cl = r.headers.get("Content-Length")
                    res.total = int(cl) if cl and cl.isdigit() else None
    except Exception:
        pass
    return res


class NetworkRetry(Exception):
    pass


class EmptyRange(NetworkRetry):
    """服务器声称 206 却返回空/缺数据 —— 典型伪 Range 行为。"""


class DownloadJob:
    """一次下载任务。GUI 与 CLI 都调用它。"""

    def __init__(self, url, save_path, threads=DEFAULT_THREADS, headers=None,
                 status_cb=None, quiet=True, save_dir=None):
        self.url = url
        self.save_path = save_path            # 最终保存路径(绝对, None=自动命名)
        self.save_dir = save_dir
        self.threads = max(1, min(64, threads))
        self.headers = {"User-Agent": UA}
        if headers:
            self.headers.update(headers)
        self.status_cb = status_cb or (lambda *a, **k: None)
        self.quiet = quiet
        self.cancel_event = threading.Event()
        self._last_emit = 0.0
        self._set_paths(save_path)
        self._done_lock = threading.Lock()
        self._segments = []                   # [[start, end, done], ...]
        self.total = None
        self.error = None
        self.finished = False
        self.server_unreliable = False

    def _set_paths(self, save_path):
        self.save_path = save_path
        if save_path:
            self.sidecar = save_path + ".dlp"
            self.part = save_path + ".part"
        else:
            self.sidecar = self.part = None

    # ---------- 断点信息 ----------
    def _load_sidecar(self):
        try:
            with open(self.sidecar, "r", encoding="utf-8") as f:
                d = json.load(f)
            if (d.get("url") == self.url and os.path.exists(self.part)
                    and d.get("total") == self.total):
                segs = d.get("segments") or []
                if segs and all(s[1] < s[0] + self.total for s in segs):
                    return segs
        except Exception:
            pass
        return None

    def _save_sidecar(self):
        try:
            with open(self.sidecar, "w", encoding="utf-8") as f:
                json.dump({"url": self.url, "total": self.total,
                           "segments": self._segments}, f)
        except Exception:
            pass

    def done_bytes(self):
        with self._done_lock:
            return sum(s[2] for s in self._segments)

    # ---------- 主流程 ----------
    def run(self):
        try:
            self._run()
        except Exception as e:
            self.error = e
        finally:
            self._emit()

    def _run(self):
        self._log(f"探测: {self.url}")
        p = probe(self.url, self.headers)
        self.total = p.total
        ranged = p.ranged

        # 决定保存路径(处理重名)
        if not self.save_path:
            self._set_paths(unique_path(
                self.save_dir or os.getcwd(),
                p.filename or guess_filename(self.url)))

        if self.total is None or self.total == 0:
            # 未知大小 → 单线程流式
            self._download_stream()
            return
        if ranged and self.total > 8192:
            # 抽样验证, 防"伪 Range"CDN(声称支持但响应不一致)
            if not _validate_range(self.url, self.headers, self.total):
                ranged = False
                self.total = None       # 它给的 total 也不可信, 交给流式重探
                self._log("服务器分段响应不一致, 改用单线程完整下载")
                self._download_stream()
                return
        if not ranged or self.total <= CHUNK:
            # 不支持 Range 或文件太小 → 单分段(仍走分段逻辑, offset=0)
            self.threads = 1
            ranged = False

        segs = self._load_sidecar()
        if segs:
            self._segments = segs
            self._log("发现断点，继续下载")
            with open(self.part, "r+b") as f:
                if os.path.getsize(self.part) < self.total:
                    f.truncate(self.total)
        else:
            n = self.threads
            size = _align_up(math.ceil(self.total / n), CHUNK)
            self._segments = []
            i = 0
            while i < self.total:
                end = min(i + size, self.total) - 1
                self._segments.append([i, end, 0])
                i = end + 1
            with open(self.part, "wb") as f:
                f.truncate(self.total)          # 预分配(NTFS 稀疏文件)
            self._save_sidecar()
            self._log(f"文件大小 {fmt_size(self.total)}，分 {len(self._segments)} 段下载")

        workers = []
        for idx, seg in enumerate(self._segments):
            if seg[2] >= seg[1] - seg[0] + 1:
                continue                        # 该段已完成
            t = threading.Thread(target=self._download_segment, args=(idx,),
                                 daemon=True)
            t.start()
            workers.append(t)
        for t in workers:
            t.join()

        if self.server_unreliable:
            # 分段反复失败: 丢弃分段数据(可能是坏数据), 改单线程完整重下
            self.cancel_event.clear()
            for f in (self.part, self.sidecar):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self._segments = []
            self.total = None
            self._log("分段响应不稳定, 改用单线程完整下载")
            self._download_stream()
            return
        if self.cancel_event.is_set():
            self._save_sidecar()
            self._log("已取消（已下载部分保留，可续传）")
            return
        if self.error:
            raise self.error

        os.replace(self.part, self.save_path)
        try:
            os.remove(self.sidecar)
        except OSError:
            pass
        self.finished = True
        self._log(f"完成: {self.save_path}")

    # ---------- 分段 worker ----------
    def _download_segment(self, idx):
        seg = self._segments[idx]
        retries = 0
        empty_streak = 0
        while not self.cancel_event.is_set():
            start = seg[0] + seg[2]
            end = seg[1]
            if start > end:
                return
            try:
                self._fetch_range(start, end, seg)
                retries = 0
                empty_streak = 0
            except EmptyRange:
                empty_streak += 1
                if empty_streak >= 2:
                    self.server_unreliable = True
                    self.cancel_event.set()
                    return
                time.sleep(1)
                continue
            except NetworkRetry:
                retries += 1
                if retries >= MAX_RETRIES:
                    # 分段不可靠(伪 Range CDN 等): 触发整体回落
                    self.server_unreliable = True
                    self.cancel_event.set()
                    return
                time.sleep(min(2 * retries, 10))
                continue
            except Exception as e:
                self.error = e
                self.cancel_event.set()
                return
            if seg[2] >= seg[1] - seg[0] + 1:
                return                          # 本段完成

    def _fetch_range(self, start, end, seg):
        h = dict(self.headers)
        h["Range"] = f"bytes={start}-{end}"
        req = urllib.request.Request(self.url, headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (416,):                # 范围越界 → 视为完成
                seg[2] = seg[1] - seg[0] + 1
                return
            if e.code in (408, 429, 500, 502, 503, 504):
                raise NetworkRetry()
            raise
        except Exception:
            raise NetworkRetry()
        with r:
            if r.status == 200 and (start > 0 or end < self.total - 1):
                # 服务器无视 Range 头却返回 200 全量: 该段数据无意义
                raise EmptyRange()
            if r.status not in (200, 206):
                raise NetworkRetry()
            expect = end - start + 1
            written = 0
            with open(self.part, "r+b") as f:
                f.seek(start)
                while written < expect:
                    if self.cancel_event.is_set():
                        break
                    try:
                        data = r.read(CHUNK)
                    except Exception:
                        raise NetworkRetry()
                    if not data:
                        if written == 0:
                            raise EmptyRange()   # 整个响应是空的 → 可疑
                        raise NetworkRetry()     # 服务器提前断开
                    f.write(data)
                    written += len(data)
                    with self._done_lock:
                        seg[2] += len(data)
                    self._emit()
            if written < expect and not self.cancel_event.is_set():
                if written == 0:
                    raise EmptyRange()
                raise NetworkRetry()

    # ---------- 未知大小 / 不支持 Range: 单线程流式 ----------
    def _download_stream(self):
        resume = os.path.getsize(self.part) if os.path.exists(self.part) else 0
        self._segments = [[0, max(resume, 0), resume]]
        retries = 0
        while not self.cancel_event.is_set():
            h = dict(self.headers)
            wrote_resume = resume > 0
            if wrote_resume:
                h["Range"] = f"bytes={resume}-"
            req = urllib.request.Request(self.url, headers=h)
            try:
                r = urllib.request.urlopen(req, timeout=TIMEOUT)
            except Exception:
                retries += 1
                if retries >= MAX_RETRIES:
                    self.error = RuntimeError("连接失败次数过多")
                    return
                time.sleep(min(2 * retries, 10))
                continue
            with r:
                if wrote_resume and r.status != 206:
                    # 服务器不支持续传: 从头重下
                    resume = 0
                    self._segments[0] = [0, 0, 0]
                    mode = "wb"
                else:
                    mode = "ab" if resume > 0 else "wb"
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit():
                    self.total = resume + int(cl)
                eof = True
                with open(self.part, mode) as f:
                    while not self.cancel_event.is_set():
                        try:
                            data = r.read(CHUNK)
                        except Exception:
                            eof = False
                            break
                        if not data:
                            break
                        f.write(data)
                        self._segments[0][2] += len(data)
                        self._segments[0][1] += len(data)
                        resume = self._segments[0][2]
                        self._emit()
                    else:
                        eof = False    # 因取消退出 while(未 break)
            done = self._segments[0][2]
            if self.cancel_event.is_set():
                self._save_sidecar()
                return
            if eof and (self.total is None or done >= self.total):
                break                       # 正常读完
            retries += 1
            if retries >= MAX_RETRIES:
                self.error = RuntimeError("下载中断, 重试次数过多")
                return
        if self.error:
            return
        if self.cancel_event.is_set():
            self._save_sidecar()
            return
        os.replace(self.part, self.save_path)
        try:
            os.remove(self.sidecar)
        except OSError:
            pass
        self.finished = True

    # ---------- 状态上报 ----------
    _last_emit = 0.0

    def _emit(self):
        now = time.monotonic()
        if now - self._last_emit < 0.2 and not self.cancel_event.is_set():
            return
        self._last_emit = now
        done = self.done_bytes()
        self.status_cb(done, self.total)

    def _log(self, msg):
        if self.quiet:
            self.status_cb(None, None, msg)
        else:
            print(msg, flush=True)


# ---------- 速度/显示工具 ----------
def fmt_size(n):
    if n is None:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0


def fmt_speed(bps):
    return f"{fmt_size(bps)}/s" if bps is not None else "-"


def fmt_eta(seconds):
    if seconds is None or seconds < 0 or seconds > 999 * 3600:
        return "--:--:--"
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class SpeedMeter:
    """滑动窗口平均速度。"""
    def __init__(self, window=5.0):
        self.window = window
        self.hist = []

    def update(self, done):
        t = time.monotonic()
        self.hist.append((t, done))
        while len(self.hist) > 1 and t - self.hist[0][0] > self.window:
            self.hist.pop(0)
        t0, d0 = self.hist[0]
        dt = t - t0
        return (done - d0) / dt if dt > 0.3 else None


# ---------- 命令行模式 ----------
def cli_download(args):
    headers = {}
    for kv in args.header or []:
        k, _, v = kv.partition(":")
        headers[k.strip()] = v.strip()

    save_dir = args.dir or os.getcwd()
    save_path = args.output
    if save_path and os.path.isdir(save_path):
        save_dir = save_path
        save_path = None
    url = args.url

    job = DownloadJob(url, save_path, args.threads, headers, quiet=False,
                      save_dir=save_dir)
    meter = SpeedMeter()
    stop = threading.Event()

    def poll():
        t0 = time.time()
        while not job.finished and job.error is None and not job.cancel_event.is_set():
            if stop.wait(0.25):
                break
            done = job.done_bytes()
            sp = meter.update(done)
            pct = f"{done / job.total * 100:5.1f}%" if job.total else "  ?  "
            bar_len = 30
            filled = int(bar_len * done / job.total) if job.total else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            eta = (job.total - done) / sp if (job.total and sp and sp > 0) else None
            line = (f"\r[{bar}] {pct} {fmt_size(done)}/{fmt_size(job.total)} "
                    f"{fmt_speed(sp)} 剩余 {fmt_eta(eta)}   ")
            sys.stdout.write(line)
            sys.stdout.flush()

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    interrupted = False
    try:
        job.run()
    except KeyboardInterrupt:
        interrupted = True
        job.cancel_event.set()
    finally:
        stop.set()
        th.join(timeout=1)
        print()
    if interrupted:
        job._save_sidecar()
        print("已暂停: 重新运行相同命令即可续传。")
        sys.exit(130)
    if job.error:
        print(f"错误: {job.error}")
        sys.exit(1)
    if job.finished:
        print(f"已保存到: {job.save_path}  ({fmt_size(os.path.getsize(job.save_path))})")
    else:
        print("已暂停: 重新运行相同命令即可续传。")
        sys.exit(130)


# ---------- 图形界面 ----------
class App:
    def __init__(self, root, embedded=False, on_complete=None):
        global tk, filedialog, ttk
        import tkinter as tk
        from tkinter import filedialog, ttk
        self.root = root
        self.on_complete = on_complete
        if not embedded:
            root.title("直链下载器")
            root.geometry("640x460")
            root.minsize(560, 420)
        self.job = None
        self.job_thread = None
        self.msg_queue = []
        self._build()
        self._tick()

    def _build(self):
        pad = dict(padx=8, pady=4)
        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="下载链接:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(self.root)
        e = ttk.Entry(frm, textvariable=self.url_var)
        e.grid(row=0, column=1, columnspan=3, sticky="we")
        self.paste_btn = ttk.Button(frm, text="粘贴", width=6,
                                     command=self._paste)
        self.paste_btn.grid(row=0, column=4, sticky="e", padx=(4, 0))
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="保存目录:").grid(row=1, column=0, sticky="w",
                                              pady=(6, 0))
        self.dir_var = tk.StringVar(self.root, value=os.path.join(
            os.path.expanduser("~"), "Downloads"))
        ttk.Entry(frm, textvariable=self.dir_var).grid(
            row=1, column=1, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Button(frm, text="浏览...", width=6, command=self._browse)\
            .grid(row=1, column=4, sticky="e", padx=(4, 0), pady=(6, 0))

        ttk.Label(frm, text="文件名:").grid(row=2, column=0, sticky="w",
                                            pady=(6, 0))
        self.name_var = tk.StringVar(self.root)
        ttk.Entry(frm, textvariable=self.name_var).grid(
            row=2, column=1, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(frm, text="线程数:").grid(row=2, column=4, sticky="e",
                                            pady=(6, 0))
        self.threads_var = tk.IntVar(self.root, value=DEFAULT_THREADS)
        sp = ttk.Spinbox(frm, from_=1, to=64, width=6,
                         textvariable=self.threads_var)
        sp.grid(row=3, column=4, sticky="e")
        ttk.Label(frm, text="(留空=自动命名)").grid(row=3, column=1,
                                                    sticky="w")

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="开始下载",
                                    command=self._start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="暂停/取消",
                                     command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.state_lbl = ttk.Label(btns, text="就绪")
        self.state_lbl.pack(side="right")

        self.bar = ttk.Progressbar(self.root, maximum=1000)
        self.bar.pack(fill="x", padx=8)
        self.stat_lbl = ttk.Label(self.root, text="", font=("Consolas", 9))
        self.stat_lbl.pack(fill="x", padx=8)

        self.log = tk.Text(self.root, height=9, state="disabled",
                           font=("Microsoft YaHei UI", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

    # -- helpers --
    def _paste(self):
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except Exception:
            pass

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or ".")
        if d:
            self.dir_var.set(d)

    def _append_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- job control --
    def _on_status(self, done, total, msg=None):
        if msg is not None:
            self.msg_queue.append(("log", msg))
        else:
            self.msg_queue.append(("prog", done, total))

    def _start(self):
        if self.job_thread and self.job_thread.is_alive():
            return
        url = self.url_var.get().strip()
        if not url:
            self._append_log("请先输入下载链接")
            return
        if not re.match(r"https?://", url, re.I):
            url = "http://" + url
        d = self.dir_var.get().strip() or os.getcwd()
        name = self.name_var.get().strip()
        if name and safe_filename(name) != name:
            self._append_log("文件名含路径或非法字符，请只填写文件名。")
            return
        save = os.path.join(d, name) if name else None
        try:
            threads = self.threads_var.get()
            os.makedirs(d, exist_ok=True)
            if save and os.path.exists(save):
                self._append_log("文件已存在，请更换文件名；原文件已保留。")
                return
        except (OSError, tk.TclError) as exc:
            self._append_log(f"无法开始下载: {exc}")
            return
        self.job = DownloadJob(url, save, threads,
                               status_cb=self._on_status, save_dir=d)
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.state_lbl.configure(text="下载中...")
        self._append_log(f"开始: {url}")
        self.job_thread = threading.Thread(target=self._job_worker,
                                           daemon=True)
        self.job_thread.start()

    def _job_worker(self):
        self.job.run()
        if self.job.error:
            self.msg_queue.append(("log", f"错误: {self.job.error}"))
            self.msg_queue.append(("done", None))
        elif self.job.finished:
            self.msg_queue.append(("done", self.job.save_path))
        else:
            self.msg_queue.append(("done", False))

    def _cancel(self):
        if self.job:
            self.job.cancel_event.set()
            self.state_lbl.configure(text="正在停止...")

    def _tick(self):
        # 处理消息队列 + 速度计算
        meter = getattr(self, "_meter", None)
        if meter is None:
            meter = self._meter = SpeedMeter()
        while self.msg_queue:
            m = self.msg_queue.pop(0)
            if m[0] == "log":
                self._append_log(m[1])
            elif m[0] == "prog":
                done, total = m[1], m[2]
                sp = meter.update(done)
                if total:
                    self.bar.configure(value=done / total * 1000)
                    pct = done / total * 100
                    eta = (total - done) / sp if sp and sp > 0 else None
                    self.stat_lbl.configure(text=(
                        f"{pct:5.1f}%  {fmt_size(done)} / {fmt_size(total)}   "
                        f"速度 {fmt_speed(sp)}   剩余 {fmt_eta(eta)}"))
                else:
                    self.stat_lbl.configure(text=(
                        f"已下载 {fmt_size(done)}   速度 {fmt_speed(sp)}"))
            elif m[0] == "done":
                path = m[1]
                self._meter = None
                if path is None:
                    self.state_lbl.configure(text="失败")
                elif path is False:
                    self.state_lbl.configure(text="已暂停，可续传")
                    self._append_log("已暂停（重新点击开始即可续传）")
                else:
                    self.bar.configure(value=1000)
                    self.state_lbl.configure(text="完成 ✔")
                    self._append_log(f"完成: {path}")
                    if self.on_complete:
                        self.on_complete(path)
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
        self._tick_after = self.root.after(120, self._tick)

    def shutdown(self):
        if self.job:
            self.job.cancel_event.set()
        self.root.after_cancel(self._tick_after)


def run_gui():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    try:
        s = ttk.Style()
        if "vista" in s.theme_names():
            s.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


# ---------- 入口 ----------
def main():
    ap = argparse.ArgumentParser(
        description="直链下载器: 多线程 HTTP 分段下载, 支持断点续传。"
                    "不带参数运行则打开图形界面。")
    ap.add_argument("url", nargs="?", help="下载直链")
    ap.add_argument("-n", "--threads", type=int, default=DEFAULT_THREADS,
                    help=f"并发线程数 (默认 {DEFAULT_THREADS})")
    ap.add_argument("-o", "--output", help="保存路径(文件或目录)")
    ap.add_argument("-d", "--dir", help="保存目录(配合自动文件名)")
    ap.add_argument("-H", "--header", action="append", metavar="名:值",
                    help="附加请求头, 如 -H \"Referer:https://xx\" 可重复")
    args = ap.parse_args()
    if not args.url:
        run_gui()
    else:
        cli_download(args)


if __name__ == "__main__":
    main()
