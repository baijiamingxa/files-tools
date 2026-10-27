#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
疾速解压 SwiftExtract
======================
高速多格式压缩/解压工具。基础 ZIP / TAR 使用 Python 标准库，
额外格式可使用工具箱自动配置的便携 7-Zip / Zstd 或已有可选引擎。

特性：
  * 解压"分流引擎"——ZIP 多文件并行 + 大文件分块并行拷贝 + 解压/写盘双线程流水线
  * 压缩"并行引擎"——大文件线程池预压缩 + 超大文件块级并行 deflate + 顺序组装
  * 压缩格式：zip(可 AES-256 加密, zip64) / 7z(可加密) / tar / tar.gz / tar.xz / tar.zst
  * 解压格式：上述全部 + ZipCrypto 加密 zip + gz/bz2/xz/zst 单文件流
  * RAR / CAB / ISO / ARJ / LZH / MSI：使用便携 7-Zip 或已有 WinRAR/UnRAR
  * 分卷：.001 合并 / RAR .partN / 多卷 zip(.z01)
  * 中文 ZIP 文件名 GBK 修复、路径穿越(zip-slip)防护、原子写入
  * 图形界面（拖拽/批量/进度/弹窗要密码/打包压缩对话框） + 命令行双模式

用法：
  图形界面:  py swift_extract.py
  解压:      py swift_extract.py --cli [-o 输出目录] [-j 线程数] 压缩包...
  压缩:      py swift_extract.py --cli -c [-L 级别] [-F 格式] -o 档案 文件/目录...
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
import zlib
import runtime_manager
import archive_runtime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

APP_NAME = "疾速解压"
APP_VER = "1.1"
CHUNK = 1 << 20          # 1 MB 流式读写块
BIG_SPLIT = 64 << 20     # ≥64MB 的存储式文件启用分块并行拷贝
BIG_PUMP = 16 << 20      # ≥16MB 的文件启用 解压线程->写盘线程 流水线
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW

# ---- 压缩 ----
COMP_BIG = 2 << 20       # ≥2MB 的文件交给压缩线程池（小文件直接压更快）
PDEF_CHUNK = 8 << 20     # 单个大文件并行 deflate 的块大小（pigz 式）
PDEF_DICT = 32 << 10     # zlib 窗口 32KB（分块字典衔接长度）
LEVELS = {"store": 0, "fast": 1, "normal": 6, "max": 9}
LEVELS_ZH = {"store": "存储", "fast": "最快", "normal": "标准", "max": "最大"}

# ---------------- 可选依赖 ----------------
try:
    import py7zr
except Exception:
    py7zr = None
try:
    import pyzipper
except Exception:
    pyzipper = None
try:
    import zstandard
except Exception:
    zstandard = None
try:
    import windnd
except Exception:
    windnd = None


# =====================================================================
# 异常与通用工具
# =====================================================================

class NeedPassword(Exception):
    """压缩包已加密，需要用户提供密码。"""


class Cancelled(Exception):
    """用户取消。"""


class EngineError(Exception):
    """解压引擎错误。"""


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit not in ("B",) else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def human_ext(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


# =====================================================================
# 后端探测：WinRAR / UnRAR
# =====================================================================

def _find_first(candidates):
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def find_winrar():
    env = os.environ.get("WINRAR_HOME")
    return _find_first([
        env,
        r"C:\Program Files\WinRAR\WinRAR.exe",
        r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
        shutil.which("WinRAR.exe"),
    ])


def find_unrar():
    env = os.environ.get("UNRAR_HOME")
    winrar_dir = os.path.dirname(find_winrar()) if find_winrar() else None
    return _find_first([
        env,
        os.path.join(winrar_dir, "UnRAR.exe") if winrar_dir else None,
        r"C:\Program Files\WinRAR\UnRAR.exe",
        shutil.which("UnRAR.exe"),
    ])


WINRAR = find_winrar()
UNRAR = find_unrar()


# =====================================================================
# 格式识别（魔数 + 分卷归组）
# =====================================================================

def _read_head(path: str, n: int = 262144) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def detect_format(path: str) -> str:
    """按内容魔数识别格式，返回内部格式标识。"""
    head = _read_head(path)
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") or head.startswith(b"PK\x06\x06"):
        return "zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zst"
    if len(head) > 265 and head[257:262] == b"ustar":
        return "tar"
    if head.startswith(b"MSCF"):
        return "cab"
    if head.startswith(b"MZ"):
        # 自解压/可执行：找内嵌档案签名
        if b"Rar!\x1a\x07" in head:
            return "rar"
        if b"7z\xbc\xaf\x27\x1c" in head:
            return "7z"
        if b"PK\x03\x04" in head:
            return "zip"
        return "exe"
    # ISO 在 0x8001 处
    with open(path, "rb") as f:
        try:
            f.seek(0x8001)
            if f.read(5) == b"CD001":
                return "iso"
        except OSError:
            pass
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "msi"
    return "unknown"


_VOL_NUM = re.compile(r"\.(\d{2,4})$")
_PART_RAR = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)


def volume_base(path: str):
    """若属于分卷组，返回 (组内规范首卷路径, 组标识)；否则 (path, None)。"""
    p = path
    m = _PART_RAR.search(p)
    if m:
        n = int(m.group(1))
        first = p[:m.start()] + ".part1.rar"
        if n != 1 and os.path.isfile(first):
            p = first
        return p, p
    m = _VOL_NUM.search(p)
    if m and 1 <= int(m.group(1)) <= 9999:
        return p[:m.start()] + ".001", p[:m.start()]
    lower = p.lower()
    if lower.endswith(".r00") and os.path.isfile(lower[:-4] + ".rar"):
        return lower[:-4] + ".rar", lower[:-4] + ".rar"
    if lower.endswith(".z01") and os.path.isfile(p[:-4] + ".zip"):
        return p[:-4] + ".zip", p[:-4] + ".zip"
    # 多卷 zip: xxx.z01 ... xxx.z99 + xxx.zip
    if lower.endswith(".zip"):
        z01 = p[:-4] + ".z01"
        if os.path.isfile(z01):
            return p, p
    return path, None


def all_volume_parts(canonical: str, group: str):
    """列出分卷组全部存在的分卷（升序）。"""
    if not group:
        return [canonical]
    m = _PART_RAR.search(canonical)
    if m:  # rar part 卷
        parts = []
        i = 1
        while True:
            q = canonical[:m.start()] + f".part{i}.rar"
            if os.path.isfile(q):
                parts.append(q)
                i += 1
            else:
                break
        if len(parts) > 1:
            return parts
        return [canonical]
    m = _VOL_NUM.search(canonical)
    if m:
        parts = []
        for i in range(1, 10000):
            q = canonical[:m.start()] + f".{i:03d}"
            if os.path.isfile(q):
                parts.append(q)
            else:
                break
        return parts or [canonical]
    # 多卷 zip
    if canonical.lower().endswith(".zip"):
        parts = []
        for i in range(1, 100):
            q = canonical[:-4] + f".z{i:02d}"
            if os.path.isfile(q):
                parts.append(q)
        parts.append(canonical)
        return parts
    return [canonical]


# =====================================================================
# 安全路径
# =====================================================================

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str) -> str:
    """清洗档案内相对路径，防 zip-slip / 非法 Windows 文件名。返回 '' 表示跳过。"""
    name = name.replace("\\", "/")
    parts = []
    for seg in name.split("/"):
        if seg in ("", ".",):
            continue
        if seg == ".." or re.fullmatch(r"[A-Za-z]:", seg):
            continue
        seg = _BAD_CHARS.sub("_", seg)
        seg = seg.rstrip(" .")  # Windows 不允许结尾空格/点
        if not seg:
            continue
        stem = seg.split(".")[0].upper()
        if stem in _RESERVED:
            seg = "_" + seg
        parts.append(seg)
    return "/".join(parts)


def fix_zip_name(raw_name: str, flag_bits: int, gbk_fix: bool) -> str:
    """旧版工具(如老 WinRAR)打包的中文 ZIP：无 UTF-8 标志位时按 GBK 重解码。"""
    if not gbk_fix or (flag_bits & 0x800):
        return raw_name
    try:
        raw = raw_name.encode("cp437")
    except UnicodeEncodeError:
        return raw_name
    if raw.isascii():
        return raw_name
    try:
        fixed = raw.decode("gbk")
    except UnicodeDecodeError:
        return raw_name
    return fixed


# =====================================================================
# 任务对象
# =====================================================================

@dataclass
class Job:
    path: str                    # 解压: 规范首卷路径 / 压缩: 输出档案路径
    kind: str = ""               # zip / tar / gzip / rar / 7z / split / 压缩格式
    backend: str = ""            # 引擎名（用于界面显示）
    mode: str = "extract"        # extract | compress
    sources: list = field(default_factory=list)   # 压缩源（文件/目录）
    level: str = "normal"        # store/fast/normal/max
    dest: str = ""
    state: str = "等待"          # 等待/解压中/压缩中/完成/失败/已取消/需要密码
    error: str = ""
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    speed: float = 0.0
    started: float = 0.0
    finished: float = 0.0
    elapsed: float = 0.0
    cancel: bool = False
    password: str = ""
    # 速度采样
    _snap_t: float = 0.0
    _snap_b: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_bytes(self, n: int):
        with self._lock:
            self.bytes_done += n

    def add_files(self, n: int = 1):
        with self._lock:
            self.files_done += n

    def tick(self):
        now = time.perf_counter()
        if self._snap_t:
            dt = now - self._snap_t
            if dt >= 0.2:
                inst = (self.bytes_done - self._snap_b) / dt
                self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
                self._snap_t, self._snap_b = now, self.bytes_done
        else:
            self._snap_t, self._snap_b = now, self.bytes_done


@dataclass
class Options:
    workers: int = 0            # 0 = 自动(CPU核数)
    parallel_jobs: int = 2      # 同时解压的压缩包数
    big_split: bool = True      # 大文件分流
    gbk_fix: bool = True
    subfolder: bool = True      # 解压到以压缩包命名的子文件夹
    outdir: str = ""            # 指定输出目录（空=压缩包所在目录）
    password: str = ""
    keep_tmp: bool = False


def auto_workers() -> int:
    n = os.cpu_count() or 4
    return max(2, min(n, 16))


# =====================================================================
# 通用：解压线程 → 写盘线程 流水线
# =====================================================================

def pump_copy(src_read, out_write, job: Job, check_cancel):
    """src_read(n) 读(解压)线程，out_write(b) 在当前线程写盘，双线程流水线。"""
    q: queue.Queue = queue.Queue(maxsize=6)

    def reader():
        try:
            while True:
                if job.cancel:
                    q.put(Cancelled())
                    return
                b = src_read(CHUNK)
                if not b:
                    q.put(None)
                    return
                q.put(b)
        except BaseException as e:  # noqa: BLE001
            q.put(e)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        item = q.get()
        if item is None:
            return
        if isinstance(item, BaseException):
            raise item
        if check_cancel():
            raise Cancelled()
        out_write(item)
        job.bytes_done += len(item)


def copy_stream(src, out, job: Job):
    """普通单线程流拷贝，带进度与取消。"""
    while True:
        if job.cancel:
            raise Cancelled()
        b = src.read(CHUNK)
        if not b:
            return
        out.write(b)
        job.bytes_done += len(b)


# =====================================================================
# 引擎 1：ZIP 分流引擎（核心）
# =====================================================================

def run_zip(job: Job, opts: Options):
    path = job.path
    zf_cls = zipfile.ZipFile

    zf = zf_cls(path)
    try:
        infos = zf.infolist()
        # 探测 AES / 加密
        has_aes = any(i.compress_type == 99 for i in infos)
        encrypted = any(i.flag_bits & 0x1 for i in infos)
        if has_aes:
            if pyzipper:
                # 关键：改用 pyzipper 后必须用它自己的 infolist（类不兼容）
                zf.close()
                zf_cls = pyzipper.AESZipFile
                zf = zf_cls(path)
                infos = zf.infolist()
            elif WINRAR:
                job.backend = "WinRAR(AES)"
                return run_external(job, opts)
            elif runtime_manager.get_tool("7zip"):
                return archive_runtime.extract(job, opts)
            else:
                raise EngineError("缺少 AES ZIP 引擎，请在工具箱“运行环境”中检测并补齐。")
        if encrypted and not job.password:
            raise NeedPassword("压缩包已加密")

        entries = []
        dirs = set()
        for info in infos:
            name = fix_zip_name(info.filename, info.flag_bits, opts.gbk_fix)
            rel = sanitize_name(name)
            if not rel:
                continue
            target = os.path.join(job.dest, *rel.split("/"))
            if info.is_dir() or name.endswith("/"):
                dirs.add(os.path.dirname(target))
                continue
            dirs.add(os.path.dirname(target))
            entries.append((info, target))

        job.files_total = len(entries)
        job.bytes_total = sum(i.file_size for i, _ in entries)

        for d in sorted(dirs):
            if d:
                os.makedirs(d, exist_ok=True)

        workers = opts.workers or auto_workers()
        workers = max(1, min(workers, max(1, len(entries))))

        # 互斥句柄池：每个任务检出一条独立句柄用完归还，绝不共享
        # （pyzipper 及旧版 zipfile 都不支持单句柄多流并发）
        handle_pool: queue.Queue = queue.Queue()
        for _ in range(workers):
            handle_pool.put(zf_cls(path))
        counter = [0]
        closed = [False]

        def close_all():
            if closed[0]:
                return
            closed[0] = True
            while True:
                try:
                    z = handle_pool.get_nowait()
                except queue.Empty:
                    return
                try:
                    z.close()
                except Exception:
                    pass

        big_jobs = []   # (info, target) 存储式大文件 → 分块并行
        tasks = []      # 每文件任务

        for info, target in entries:
            if (opts.big_split and info.compress_type == zipfile.ZIP_STORED
                    and info.file_size >= BIG_SPLIT):
                big_jobs.append((info, target))
            else:
                tasks.append((info, target))

        for info, target in big_jobs:
            n = info.file_size
            step = max(16 << 20, -(-n // (workers * 2)))
            for off in range(0, n, step):
                tasks.append((info, target, off, min(step, n - off)))
                if job.cancel:
                    raise Cancelled()
            # 不预创建/截断文件：range 任务 os.write 越过 EOF 会自动补零扩展，
            # 而 truncate(150MB) 在 NTFS 上要逐块填零，反而更慢
            os.makedirs(os.path.dirname(target) or job.dest, exist_ok=True)

        # 大任务先跑：让并行分块尽早铺开，小文件收尾
        tasks.sort(key=lambda t: (t[3] if len(t) == 4 else t[0].file_size), reverse=True)

        errors = []
        errs_lock = threading.Lock()

        def do_file(info, target):
            if job.cancel:
                raise Cancelled()
            h = handle_pool.get()
            try:
                pwd = job.password.encode() if job.password else None
                with h.open(info, pwd=pwd) as src:
                    if info.file_size >= BIG_PUMP:
                        # 大文件走 临时文件→原子改名，避免留下半个文件
                        tmp = target + ".part~"
                        try:
                            with open(tmp, "wb") as out:
                                pump_copy(src.read, out.write, job, lambda: job.cancel)
                            os.replace(tmp, target)
                        except BaseException:
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                            raise
                    else:
                        # 小文件直写目标（省去中转文件的系统调用与杀软扫描开销）
                        with open(target, "wb") as out:
                            copy_stream(src, out, job)
                job.files_done += 1
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                if "password" in str(e).lower():
                    raise NeedPassword(f"{info.filename}: 密码错误") from e
                with errs_lock:
                    errors.append(f"{info.filename}: {e}")
            finally:
                handle_pool.put(h)

        def do_range(info, target, off, length):
            if job.cancel:
                raise Cancelled()
            h = handle_pool.get()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0))
            try:
                os.lseek(fd, off, os.SEEK_SET)
                with h.open(info) as src:
                    src.seek(off)  # 存储式条目 seek 零成本
                    remain = length
                    while remain > 0:
                        if job.cancel:
                            raise Cancelled()
                        b = src.read(min(CHUNK * 4, remain))
                        if not b:
                            break
                        os.write(fd, b)
                        remain -= len(b)
                        job.bytes_done += len(b)
            finally:
                os.close(fd)
                handle_pool.put(h)
            with errs_lock:
                counter[0] += 1

        def run(task):
            if len(task) == 4:
                do_range(*task)
            else:
                do_file(*task)

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(run, t) for t in tasks]
                try:
                    for f in as_completed(futs):
                        f.result()
                except (Cancelled, Exception):  # noqa: BLE001
                    for f in futs:
                        f.cancel()
                    raise
        finally:
            close_all()
        zf.close()

        # 大文件 files_done 记账
        job.files_done += len(big_jobs) if not errors else 0
        if errors:
            msg = "; ".join(errors[:3])
            if len(errors) > 3:
                msg += f" 等 {len(errors)} 项"
            if job.files_done == 0 and job.files_total:
                raise EngineError(msg)
            job.error = f"{len(errors)} 项失败: {msg}"
    finally:
        try:
            zf.close()
        except Exception:
            pass


# =====================================================================
# 引擎 2：TAR / 单文件压缩流 引擎
# =====================================================================

_TAR_SUFFIX_RE = re.compile(r"(\.tar)?\.(gz|bz2|xz|zst|tgz|tbz2|txz|tzst)$", re.IGNORECASE)


def archive_stem(path: str) -> str:
    """压缩包去后缀名（含 .part1.rar / .zip.001 / .tar.gz 等组合后缀）。"""
    base = os.path.basename(path)
    # 分卷后缀
    m = _PART_RAR.search(base)
    if m:
        base = base[:m.start()]
        return base
    m = _VOL_NUM.search(base)
    if m:
        base = base[:m.start()]
    low = base.lower()
    if low.endswith(".tar"):
        return base[:-4]
    m2 = _TAR_SUFFIX_RE.search(base)
    if m2 and m2.group(1):  # xxx.tar.gz → xxx
        return base[:m2.start()]
    # 单纯压缩后缀 foo.txt.gz → foo.txt；foo.gz → foo
    root, ext = os.path.splitext(base)
    if ext.lower() in (".gz", ".bz2", ".xz", ".zst", ".lz4", ".br"):
        base2 = root
        root2, ext2 = os.path.splitext(base2)
        return base2 if ext2.lower() in (".tar", ".txt", "") or not root2 else base2
    root, ext = os.path.splitext(base)
    return root or base


class _PeekStream(io.RawIOBase):
    """把已预读的头部字节拼回流前，供流式 tar 读取。"""

    def __init__(self, head: bytes, raw):
        self._buf = head
        self._raw = raw

    def readable(self):
        return True

    def read(self, n=-1):
        if self._buf:
            if n is None or n < 0:
                n = len(self._buf)
            b = self._buf[:n]
            self._buf = self._buf[n:]
            return b
        return self._raw.read(n if n and n > 0 else CHUNK)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def _gzip_isize(path: str) -> int:
    """读 gzip 尾部 ISIZE（解压后大小 mod 4GB），用于总进度估算。"""
    try:
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            return struct.unpack("<I", f.read(4))[0]
    except OSError:
        return 0


def _open_decompressed(path: str, kind: str):
    f = open(path, "rb")
    if kind == "gzip":
        return f, gzip.GzipFile(fileobj=f)
    if kind == "bzip2":
        return f, bz2.BZ2File(f)
    if kind == "xz":
        return f, lzma.LZMAFile(f)
    if kind == "zst":
        if zstandard:
            return f, zstandard.ZstdDecompressor().stream_reader(f)
        raise EngineError("缺少 zstandard 库，无法解压 zst")
    return f, f  # 纯 tar


def _extract_tar_members(tf: tarfile.TarFile, members, job: Job):
    """members=None 时为流式模式：边迭代边解压（extractfile 只对当前成员有效）。"""
    it = iter(members) if members is not None else iter(tf)
    for m in it:
        if job.cancel:
            raise Cancelled()
        rel = sanitize_name(m.name)
        if not rel:
            continue
        target = os.path.join(job.dest, *rel.split("/"))
        if m.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        if not m.isreg():
            continue  # 跳过符号链接/设备文件等
        src = tf.extractfile(m)
        if src is None:
            continue
        os.makedirs(os.path.dirname(target) or job.dest, exist_ok=True)
        tmp = target + ".part~"
        try:
            with src, open(tmp, "wb") as out:
                if m.size >= BIG_PUMP:
                    pump_copy(src.read, out.write, job, lambda: job.cancel)
                else:
                    copy_stream(src, out, job)
            os.replace(tmp, target)
            job.files_done += 1
        except Cancelled:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise EngineError(f"{m.name}: {e}") from e


def run_stream(job: Job, opts: Options):
    """tar / tar.gz / tgz / 单文件 gz|bz2|xz|zst 统一流式解压。"""
    path, kind = job.path, job.kind
    os.makedirs(job.dest, exist_ok=True)

    if kind == "tar":  # 未压缩 tar 可随机访问：先建索引拿到精确总量
        with tarfile.open(path, "r:") as tf:
            members = tf.getmembers()
            job.files_total = sum(1 for m in members if m.isreg())
            job.bytes_total = sum(m.size for m in members if m.isreg())
            _extract_tar_members(tf, members, job)
        return

    src_file, decomp = _open_decompressed(path, kind)
    try:
        head = decomp.read(512)
        is_tar = len(head) >= 262 and head[257:262] == b"ustar"
        if is_tar:
            if kind == "gzip":  # 用 ISIZE 估算总量，进度条更友好
                est = _gzip_isize(path)
                if 0 < est < (4 << 30):
                    job.bytes_total = est
            tf = tarfile.open(fileobj=_PeekStream(head, decomp), mode="r|")
            with tf:
                _extract_tar_members(tf, None, job)  # 流式：总量边解压边累计
        else:
            # 单文件压缩流：foo.txt.gz → foo.txt
            out_name = archive_stem(os.path.basename(path)) or "extracted"
            target = os.path.join(job.dest, out_name)
            base, ext_ = os.path.splitext(target)
            n = 1
            while os.path.exists(target):
                target = f"{base}({n}){ext_}"
                n += 1
            job.files_total = 1
            tmp = target + ".part~"
            with open(tmp, "wb") as out:
                if head:
                    out.write(head)
                    job.bytes_done += len(head)
                pump_copy(decomp.read, out.write, job, lambda: job.cancel)
            os.replace(tmp, target)
            job.files_done = 1
    finally:
        try:
            decomp.close()
        except Exception:
            pass
        try:
            src_file.close()
        except Exception:
            pass


# =====================================================================
# 引擎 3：外部内核（WinRAR / UnRAR）
# =====================================================================

def _walk_stat(root: str):
    total = count = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return total, count


def _external_list(exe: str, archive: str) -> int:
    """用 lb 子命令取档案内文件数（无本地化问题）。失败返回 0。"""
    try:
        r = subprocess.run(
            [exe, "lb", "-y", "-p-", "--", archive],
            capture_output=True, creationflags=NO_WINDOW, timeout=60)
        names = r.stdout.decode("utf-8", "replace").splitlines()
        return sum(1 for n in names if n.strip() and not n.strip().endswith(("/", "\\")))
    except Exception:
        return 0


def run_external(job: Job, opts: Options, exe: str | None = None, label: str | None = None):
    """调用 WinRAR/UnRAR 解压，进度用目标目录轮询统计。"""
    if exe is None:
        exe = UNRAR if (job.kind == "rar" and UNRAR) else (WINRAR or UNRAR)
    if not exe:
        raise EngineError("未找到 WinRAR/UnRAR，无法解压该格式")
    job.backend = label or os.path.splitext(os.path.basename(exe))[0]

    if not job.files_total:
        job.files_total = _external_list(exe, job.path)

    dest = job.dest.rstrip("\\/") + os.sep
    os.makedirs(job.dest, exist_ok=True)

    is_winrar = os.path.basename(exe).lower().startswith("winrar")
    pwd = f"-p{job.password}" if job.password else "-p-"
    cmd = [exe, "x", "-y", "-o+", "-inul"]
    if is_winrar:
        cmd.append("-ibck")
    cmd += [pwd, "--", job.path, dest]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        while proc.poll() is None:
            if job.cancel:
                proc.kill()
                raise Cancelled()
            time.sleep(0.25)
            total, count = _walk_stat(job.dest)
            job.bytes_done = total
            job.files_done = min(count, job.files_total or count)
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    rc = proc.returncode
    total, count = _walk_stat(job.dest)
    job.bytes_done, job.files_done = total, (job.files_total and min(count, job.files_total)) or count

    if rc in (0, 1):
        return
    if rc == 11 or (rc == 3 and job.password):
        raise NeedPassword(f"返回码 {rc}")
    raise EngineError(f"{os.path.basename(exe)} 返回码 {rc}")


# =====================================================================
# 引擎 4：py7zr 备用 7z 引擎
# =====================================================================

def run_py7zr(job: Job, opts: Options):
    if not py7zr:
        raise EngineError("无 WinRAR 且未安装 py7zr，无法解压 7z")
    job.backend = "py7zr"
    try:
        with py7zr.SevenZipFile(job.path, mode="r", password=job.password or None) as z:
            try:
                infos = z.list()
                job.files_total = sum(1 for i in infos if not i.is_directory)
                job.bytes_total = sum(i.uncompressed for i in infos if not i.is_directory)
            except Exception:
                pass
            if job.password == "" and z.needs_password():
                raise NeedPassword("7z 档案已加密")
            z.extractall(path=job.dest)
            if job.cancel:
                raise Cancelled()
            job.files_done = job.files_total
            job.bytes_done = job.bytes_total or _walk_stat(job.dest)[0]
    except py7zr.exceptions.PasswordRequired:
        raise NeedPassword("7z 档案已加密") from None
    except py7zr.exceptions.Bad7zFile as e:
        raise EngineError(f"7z 档案损坏: {e}") from e


# =====================================================================
# 引擎 5：通用分卷合并（.001/.002… 字节分卷 → 临时合并 → 再解压）
# =====================================================================

class _CancelRef:
    """把外层任务的取消标志按引用传给内层任务（bool 赋值是拷贝）。"""

    def __init__(self, outer: "Job"):
        self.outer = outer

    def __bool__(self):
        return self.outer.cancel


def run_split_concat(job: Job, opts: Options):
    canonical, group = volume_base(job.path)
    parts = all_volume_parts(canonical, group)
    if len(parts) <= 1:
        # 单文件的 .001 未必是分卷，直接当普通档案识别
        job.kind = detect_format(job.path)
        return dispatch_engine(job, opts)

    first_magic = _read_head(parts[0], 16)
    if first_magic.startswith(b"Rar!\x1a\x07"):
        # RAR 分卷绝不能拼接合并（每卷是独立档案），把首卷直接交给 UnRAR
        job.kind = "rar"
        return dispatch_engine(job, opts)
    if canonical.lower().endswith(".zip"):
        # 多卷 zip（.z01+.zip）：zipfile 不支持跨卷，交给扩展引擎。
        if runtime_manager.get_tool("7zip"):
            job.path, job.kind = canonical, "zip"
            return archive_runtime.extract(job, opts)
        if WINRAR:
            job.kind = "zip"
            return run_external(job, opts, WINRAR, "WinRAR")
        raise EngineError("多卷 ZIP 需要扩展引擎，请在“运行环境”中检测并补齐。")

    job.backend = "合并分卷"
    job.bytes_total = sum(os.path.getsize(p) for p in parts)
    tmp_dir = os.path.join(job.dest, ".merging~")
    os.makedirs(tmp_dir, exist_ok=True)
    merged = os.path.join(tmp_dir, os.path.basename(group) + ".merged")
    try:
        with open(merged, "wb") as out:
            for p in parts:
                with open(p, "rb") as f:
                    copy_stream(f, out, job)  # 复用进度：合并也算工作量
        inner = Job(path=merged, kind=detect_format(merged),
                    dest=job.dest, password=job.password)
        inner.cancel = _CancelRef(job)
        dispatch_engine(inner, opts)
        job.files_done += inner.files_done
        job.files_total = inner.files_total or job.files_total
        if inner.bytes_total:
            job.bytes_total = job.bytes_total + inner.bytes_total
        job.bytes_done = job.bytes_total  # 合并+解压均完成
        if inner.error:
            job.error = inner.error
    finally:
        if not opts.keep_tmp:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
# 压缩引擎（纯 Python，无外部软件依赖）
# =====================================================================

_FMT_BY_EXT = {
    ".zip": "zip", ".7z": "7z", ".tar": "tar",
    ".tar.gz": "tar.gz", ".tgz": "tar.gz",
    ".tar.xz": "tar.xz", ".txz": "tar.xz",
    ".tar.zst": "tar.zst", ".tzst": "tar.zst",
}


def fmt_from_ext(path: str) -> str:
    low = path.lower()
    for ext, fmt in sorted(_FMT_BY_EXT.items(), key=lambda kv: -len(kv[0])):
        if low.endswith(ext):
            return fmt
    return ""


def _gather_entries(sources):
    """收集压缩源 → [(abs_path, arcname, size, is_dir)]，arcname 去重。"""
    entries = []
    used = set()

    def uniq(name):
        if name not in used:
            used.add(name)
            return name
        base, ext = os.path.splitext(name)
        n = 1
        while f"{base}({n}){ext}" in used:
            n += 1
        name = f"{base}({n}){ext}"
        used.add(name)
        return name

    for s in sources:
        s = os.path.abspath(s)
        if os.path.isdir(s):
            root = os.path.basename(s.rstrip("\\/")) or "archive"
            entries.append((s, uniq(root), 0, True))
            for root_dir, dirs, files in os.walk(s):
                rel = os.path.relpath(root_dir, s).replace(os.sep, "/")
                prefix = root if rel == "." else f"{root}/{rel}"
                for d in dirs:
                    entries.append((os.path.join(root_dir, d), uniq(f"{prefix}/{d}"), 0, True))
                for fn in files:
                    p = os.path.join(root_dir, fn)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    entries.append((p, uniq(f"{prefix}/{fn}"), size, False))
        elif os.path.isfile(s):
            entries.append((s, uniq(os.path.basename(s)), os.path.getsize(s), False))
    return entries


def _dos_time(mtime: float):
    t = time.localtime(mtime)
    year = min(max(t.tm_year, 1980), 2107)
    ddate = ((year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    dtime = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return ddate, dtime


def _crc_of_file(abspath: str) -> int:
    crc = 0
    with open(abspath, "rb") as f:
        while True:
            b = f.read(4 << 20)
            if not b:
                break
            crc = zlib.crc32(b, crc)
    return crc & 0xFFFFFFFF


def _deflate_chunk_task(abspath, off, length, level, part_path, is_last):
    """worker：把 [off, off+length) 压成 raw deflate 写入 part_path。
    每块用独立压缩器（Python zlib 不暴露 deflateSetDictionary，做不了 pigz 式
    字典衔接）：新块不引用前块数据，与解压器状态无关，拼接仍是合法 deflate 流，
    仅损失少量跨块压缩率。块间 Z_SYNC_FLUSH、末块 Z_FINISH。返回 csize。"""
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    csize = 0
    with open(abspath, "rb") as f, open(part_path, "wb") as out:
        f.seek(off)
        remain = length
        while remain > 0:
            b = f.read(min(1 << 20, remain))
            if not b:
                break
            out.write(c.compress(b))
            remain -= len(b)
        out.write(c.flush() if is_last else c.flush(zlib.Z_SYNC_FLUSH))
        csize = out.tell()
    return csize


def _compress_entry(task):
    """worker：把一个大文件（≥COMP_BIG）压成 raw deflate 临时分块。
    ≥2 块时块间并行（pigz 式字典衔接）。返回 dict:
      method=8: parts=[(path, csize)...], usize
      method=0: 压缩无收益，直接存原文件
    CRC 由独立读取任务计算（避免分块 CRC 合并的复杂度）。"""
    entry, level, parts_dir, job, workers, pdef_chunk, big_slots = task
    abspath, arcname, usize, _ = entry
    key = f"{abs(hash(abspath)) & 0xffffff:x}"

    with big_slots:
        if usize >= pdef_chunk * 2 and workers > 1:
            n_chunks = -(-usize // pdef_chunk)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                crc_fut = ex.submit(_crc_of_file, abspath)
                chunk_futs = []
                for i in range(n_chunks):
                    off = i * pdef_chunk
                    length = min(pdef_chunk, usize - off)
                    part = os.path.join(parts_dir, f"{key}_{off:x}.p")
                    chunk_futs.append(ex.submit(
                        _deflate_chunk_task, abspath, off, length,
                        level, part, i == n_chunks - 1))
                crc = crc_fut.result()
                parts = [(os.path.join(parts_dir, f"{key}_{i * pdef_chunk:x}.p"),
                          cf.result()) for i, cf in enumerate(chunk_futs)]
            csize = sum(cs for _, cs in parts)
            job.add_bytes(usize)
            return {"method": 8, "crc": crc, "csize": csize, "usize": usize, "parts": parts}
        # 整文件单流压缩
        part = os.path.join(parts_dir, f"{key}_s.p")
        crc = 0
        c = zlib.compressobj(level, zlib.DEFLATED, -15)
        with open(abspath, "rb") as f, open(part, "wb") as out:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                crc = zlib.crc32(b, crc)
                out.write(c.compress(b))
            out.write(c.flush())
            csize = out.tell()
        crc &= 0xFFFFFFFF
        job.add_bytes(usize)
        if csize >= usize:
            return {"method": 0, "crc": crc, "csize": usize, "usize": usize, "parts": []}
        return {"method": 8, "crc": crc, "csize": csize, "usize": usize, "parts": [part]}


class _ZipBuilder:
    """手工 ZIP 写入器（zip64 / UTF-8 名 / 目录项），配合并行预压缩产物。"""

    def __init__(self, fh):
        self.fh = fh
        self.offset = 0
        self.records = []
        self.count = 0

    def _w(self, b):
        self.fh.write(b)
        self.offset += len(b)

    def add(self, arcname, method, crc, csize, usize, chunks, mtime, is_dir=False):
        name = arcname.replace("\\", "/")
        if is_dir and not name.endswith("/"):
            name += "/"
        raw = name.encode("utf-8")
        flag = 0x800 if not name.isascii() else 0
        ddate, dtime = _dos_time(mtime)
        z64 = usize >= 0xFFFFFFFF or csize >= 0xFFFFFFFF
        ver = 45 if z64 else 20
        if z64:
            u_f = c_f = 0xFFFFFFFF
            extra = struct.pack("<HHQQ", 0x0001, 16, usize, csize)
        else:
            u_f, c_f = usize, csize
            extra = b""
        head_off = self.offset
        self._w(struct.pack("<IHHHHHIIIHH", 0x04034B50, ver, flag, method, dtime, ddate,
                            crc, c_f, u_f, len(raw), len(extra)) + raw + extra)
        for b in chunks:
            self._w(b)
        # 中央目录记录（zip64 时 usize/csize/offset 合并进一个 0x0001 extra）
        need = []
        if z64:
            need += [usize, csize]
        if head_off >= 0xFFFFFFFF:
            need += [head_off]
        cextra = (struct.pack("<HH", 0x0001, 8 * len(need)) +
                  b"".join(struct.pack("<Q", v) for v in need)) if need else b""
        cver = 45 if (z64 or head_off >= 0xFFFFFFFF) else 20
        eattr = 0x10 if is_dir else 0x20
        self.records.append(struct.pack(
            "<IHHHHHHIIIHHHHHII", 0x02014B50, cver, ver, flag, method, dtime, ddate,
            crc, c_f, u_f, len(raw), len(cextra), 0, 0, 0, eattr,
            0xFFFFFFFF if head_off >= 0xFFFFFFFF else head_off) + raw + cextra)
        self.count += 1

    def finish(self):
        cd_off = self.offset
        for rec in self.records:
            self._w(rec)
        cd_size = self.offset - cd_off
        if self.count >= 0xFFFF or cd_off >= 0xFFFFFFFF or cd_size >= 0xFFFFFFFF:
            self._w(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0,
                                self.count, self.count, cd_size, cd_off))
            self._w(struct.pack("<IQII", 0x07064B50, cd_off, 0, 0))
        self._w(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0,
                            min(self.count, 0xFFFF), min(self.count, 0xFFFF),
                            min(cd_size, 0xFFFFFFFF), min(cd_off, 0xFFFFFFFF), 0))


def run_zip_compress(job: Job, opts: Options):
    """并行 ZIP 压缩：大文件线程池预压缩 → 写入线程按序组装。"""
    out = job.path
    parent = os.path.dirname(out) or "."
    os.makedirs(parent, exist_ok=True)
    entries = _gather_entries(job.sources)
    if not entries:
        raise EngineError("没有可压缩的内容")
    job.files_total = len(entries)
    job.bytes_total = sum(e[2] for e in entries)
    level = LEVELS.get(job.level, 6)
    workers = max(1, (opts.workers or auto_workers()))
    store_only = level == 0

    parts_dir = os.path.join(parent, f".swiftparts~{os.getpid()}")
    os.makedirs(parts_dir, exist_ok=True)
    tmp_out = out + ".part~"
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        big_slots = threading.Semaphore(2)  # 同时做块级并行的大文件数
        futs = {}
        for e in entries:
            if not e[3] and e[2] >= COMP_BIG and not store_only:
                futs[e] = pool.submit(_compress_entry,
                                      (e, level, parts_dir, job, workers, PDEF_CHUNK, big_slots))

        with open(tmp_out, "wb") as fh:
            zb = _ZipBuilder(fh)
            for entry in entries:
                if job.cancel:
                    raise Cancelled()
                abspath, arcname, size, is_dir = entry
                if is_dir:
                    zb.add(arcname, 0, 0, 0, 0, (), time.time(), is_dir=True)
                    job.add_files()
                    continue
                fut = futs.get(entry)
                if fut is not None:
                    rec = fut.result()
                    if rec["method"] == 8:
                        def chunk_iter(parts=rec["parts"]):
                            for p, _cs in parts:
                                with open(p, "rb") as pf:
                                    while True:
                                        b = pf.read(CHUNK)
                                        if not b:
                                            break
                                        yield b
                        zb.add(arcname, 8, rec["crc"], rec["csize"], rec["usize"],
                               chunk_iter(), os.path.getmtime(abspath))
                    else:  # 压缩无收益 → 存原文件
                        def raw_iter(ap=abspath):
                            with open(ap, "rb") as sf:
                                while True:
                                    b = sf.read(CHUNK)
                                    if not b:
                                        return
                                    yield b
                        zb.add(arcname, 0, rec["crc"], rec["usize"], rec["usize"],
                               raw_iter(), os.path.getmtime(abspath))
                    for p, _cs in rec["parts"]:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    job.add_files()
                    continue
                # 小文件：写入线程直接压
                with open(abspath, "rb") as f:
                    raw = f.read()
                crc = zlib.crc32(raw) & 0xFFFFFFFF
                if store_only or not raw:
                    payload, method = raw, 0
                else:
                    comp = zlib.compress(raw, level)[2:-4]  # 去 zlib 头尾 → raw deflate
                    if len(comp) < len(raw):
                        payload, method = comp, 8
                    else:
                        payload, method = raw, 0
                zb.add(arcname, method, crc, len(payload), len(raw), (payload,),
                       os.path.getmtime(abspath))
                job.add_bytes(len(raw))
                job.add_files()
            zb.finish()
        pool.shutdown(wait=True)
        if job.cancel:
            raise Cancelled()
        os.replace(tmp_out, out)
        job.bytes_done = job.bytes_total
    finally:
        pool.shutdown(wait=False)
        shutil.rmtree(parts_dir, ignore_errors=True)
        try:
            os.remove(tmp_out)
        except OSError:
            pass


def run_zip_aes_compress(job: Job, opts: Options):
    """加密 zip（WinZip AES-256，pyzipper 单线程写）。"""
    if not pyzipper:
        raise EngineError("缺少 AES ZIP 引擎，请在“运行环境”中检测并补齐。")
    entries = _gather_entries(job.sources)
    if not entries:
        raise EngineError("没有可压缩的内容")
    job.files_total = len(entries)
    job.bytes_total = sum(e[2] for e in entries)
    level = LEVELS.get(job.level, 6)
    tmp_out = job.path + ".part~"
    try:
        with pyzipper.AESZipFile(tmp_out, "w", compression=pyzipper.ZIP_DEFLATED,
                                 compresslevel=level or None,
                                 encryption=pyzipper.WZ_AES) as z:
            z.setpassword(job.password.encode("utf-8"))
            for abspath, arcname, size, is_dir in entries:
                if job.cancel:
                    raise Cancelled()
                if is_dir:
                    z.writestr(arcname + "/", b"")
                else:
                    z.write(abspath, arcname, compresslevel=level or None)
                    with open(abspath, "rb") as f:
                        while True:
                            b = f.read(CHUNK)
                            if not b:
                                break
                            job.add_bytes(len(b))
                job.add_files()
        if job.cancel:
            raise Cancelled()
        os.replace(tmp_out, job.path)
        job.bytes_done = job.bytes_total
    finally:
        try:
            os.remove(tmp_out)
        except OSError:
            pass


def run_tar_compress(job: Job, opts: Options):
    """tar / tar.gz / tar.xz / tar.zst 压缩（流式）。"""
    fmt = job.kind
    entries = _gather_entries(job.sources)
    if not entries:
        raise EngineError("没有可压缩的内容")
    job.files_total = len(entries)
    job.bytes_total = sum(e[2] for e in entries)
    level = LEVELS.get(job.level, 6)
    tmp_out = job.path + ".part~"
    raw_fh = comp_fh = None
    try:
        if fmt == "tar":
            tf_ctx = tarfile.open(tmp_out, mode="w|", format=tarfile.GNU_FORMAT)
        else:
            raw_fh = open(tmp_out, "wb")
            if fmt == "tar.gz":
                comp_fh = gzip.GzipFile(fileobj=raw_fh, mode="wb",
                                        compresslevel=level or 1, mtime=0)
            elif fmt == "tar.xz":
                comp_fh = lzma.LZMAFile(raw_fh, mode="wb", preset=level or 1)
            elif fmt == "tar.zst":
                if not zstandard:
                    raise EngineError("缺少 Zstd 引擎，请在“运行环境”中检测并补齐。")
                zlvl = {0: 1, 1: 1, 6: 3, 9: 19}[level]
                comp_fh = zstandard.ZstdCompressor(level=zlvl).stream_writer(raw_fh)
            else:
                raise EngineError(f"未知 tar 格式: {fmt}")
            tf_ctx = tarfile.open(fileobj=comp_fh, mode="w|", format=tarfile.GNU_FORMAT)
        with tf_ctx as tf:
            for abspath, arcname, size, is_dir in entries:
                if job.cancel:
                    raise Cancelled()
                tf.add(abspath, arcname=arcname, recursive=False)
                if not is_dir:
                    job.add_bytes(size)
                job.add_files()
        if job.cancel:
            raise Cancelled()
        if comp_fh:
            comp_fh.close()
        if raw_fh:
            raw_fh.close()
        os.replace(tmp_out, job.path)
        job.bytes_done = job.bytes_total
    finally:
        for fh in (comp_fh, raw_fh):
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
        try:
            os.remove(tmp_out)
        except OSError:
            pass


def run_7z_compress(job: Job, opts: Options):
    """7z 压缩（py7zr，纯 Python；可加密 AES-256）。"""
    if not py7zr:
        raise EngineError("缺少 7z 引擎，请在“运行环境”中检测并补齐。")
    entries = _gather_entries(job.sources)
    if not entries:
        raise EngineError("没有可压缩的内容")
    job.files_total = len(entries)
    job.bytes_total = sum(e[2] for e in entries)
    preset = {"store": None, "fast": 1, "normal": 6,
              "max": 9 | py7zr.PRESET_EXTREME}[job.level]
    tmp_out = job.path + ".part~"
    try:
        kw = {}
        if preset is None:
            kw["filters"] = [{"id": py7zr.FILTER_COPY}]
        else:
            kw["filters"] = [{"id": py7zr.FILTER_LZMA2, "preset": preset}]
        if job.password:
            kw["password"] = job.password
        with py7zr.SevenZipFile(tmp_out, "w", **kw) as z:
            for abspath, arcname, size, is_dir in entries:
                if job.cancel:
                    raise Cancelled()
                if is_dir:
                    continue  # 目录结构由文件 arcname 承载
                z.write(abspath, arcname)
                job.add_bytes(size)
                job.add_files()
        if job.cancel:
            raise Cancelled()
        os.replace(tmp_out, job.path)
        job.bytes_done = job.bytes_total
    finally:
        try:
            os.remove(tmp_out)
        except OSError:
            pass


def run_compress(job: Job, opts: Options):
    fmt = job.kind or fmt_from_ext(job.path) or "zip"
    job.kind = fmt
    if ((fmt == "7z" and not py7zr) or (fmt == "zip" and job.password and not pyzipper)):
        if runtime_manager.get_tool("7zip"):
            return archive_runtime.compress(job, opts)
    if fmt == "tar.zst" and not zstandard and runtime_manager.get_tool("zstd"):
        return archive_runtime.compress_tar_zstd(job, opts)
    if fmt == "zip":
        if job.password:
            return run_zip_aes_compress(job, opts)
        return run_zip_compress(job, opts)
    if fmt == "7z":
        return run_7z_compress(job, opts)
    if fmt in ("tar", "tar.gz", "tar.xz", "tar.zst"):
        return run_tar_compress(job, opts)
    raise EngineError(f"不支持的压缩格式: {fmt}")


# =====================================================================
# 引擎路由
# =====================================================================

def dispatch_engine(job: Job, opts: Options):
    kind = job.kind or detect_format(job.path)
    job.kind = kind
    if kind == "zip":
        job.backend = "分流引擎"
        return run_zip(job, opts)
    if kind in ("tar", "gzip", "bzip2", "xz", "zst"):
        job.backend = "流式引擎"
        if kind == "zst" and not zstandard:
            if runtime_manager.get_tool("7zip"):
                return archive_runtime.extract(job, opts)
            if WINRAR:
                return run_external(job, opts, WINRAR, "WinRAR")
            raise EngineError("缺少 Zstd 解压引擎，请在“运行环境”中检测并补齐。")
        return run_stream(job, opts)
    if kind == "7z":
        # 优先纯 Python 引擎（无外部软件依赖）；性能需求可用 WINRAR_HOME 覆盖
        if py7zr:
            return run_py7zr(job, opts)
        if runtime_manager.get_tool("7zip"):
            return archive_runtime.extract(job, opts)
        if WINRAR:
            return run_external(job, opts, WINRAR, "WinRAR")
        raise EngineError("缺少 7z 引擎，请在“运行环境”中检测并补齐。")
    if kind == "rar":
        if runtime_manager.get_tool("7zip"):
            return archive_runtime.extract(job, opts)
        if UNRAR or WINRAR:
            return run_external(job, opts, label="UnRAR" if UNRAR else "WinRAR")
        raise EngineError("缺少 RAR 引擎，请在“运行环境”中检测并补齐。")
    if kind in ("cab", "iso", "arj", "lzh", "uue", "jar", "lz", "msi", "exe", "unknown"):
        if runtime_manager.get_tool("7zip"):
            return archive_runtime.extract(job, opts)
        if WINRAR or UNRAR:
            return run_external(job, opts, label="WinRAR" if WINRAR else "UnRAR")
        raise EngineError(f"格式 {kind} 需要扩展引擎，请在“运行环境”中检测并补齐。")
    raise EngineError(f"未知格式: {kind}")


def execute_task(job: Job, opts: Options):
    """带密码重试的统一入口（解压与压缩共用）。"""
    state_word = "压缩中" if job.mode == "compress" else "解压中"
    for attempt in range(3):
        job.started = job.started or time.perf_counter()
        try:
            if job.mode == "compress":
                job.state = state_word
                run_compress(job, opts)
            elif job.kind == "split":
                job.state = state_word
                run_split_concat(job, opts)
            else:
                job.state = state_word
                dispatch_engine(job, opts)
            job.state = "完成" if not job.error else "完成(有警告)"
            job.finished = time.perf_counter()
            job.elapsed = job.finished - job.started
            job.speed = job.bytes_total / job.elapsed if job.bytes_total and job.elapsed > 0 else job.speed
            return
        except NeedPassword as e:
            pwd = _request_password(job, opts)
            if pwd is None:
                job.state = "需要密码"
                job.error = f"需要密码({e})"
                job.finished = time.perf_counter()
                return
            job.password = pwd
            job.bytes_done = 0
            job.files_done = 0
            continue
        except Cancelled:
            job.state = "已取消"
            job.finished = time.perf_counter()
            return
        except (EngineError, Exception) as e:  # noqa: BLE001
            job.state = "失败"
            job.error = str(e)[:300]
            job.finished = time.perf_counter()
            return
    job.state = "失败"
    job.error = "密码错误"


# ---------------- 密码询问（GUI 线程安全 / CLI 终端） ----------------

_pwd_prompt = None  # GUI 注册: fn(job)->str|None


def _request_password(job: Job, opts: Options) -> str | None:
    if opts.password:
        return opts.password
    if _pwd_prompt is not None:
        return _pwd_prompt(job)
    try:
        pwd = input(f"\n[需要密码] {os.path.basename(job.path)}: ").strip()
        return pwd or None
    except EOFError:
        return None


# =====================================================================
# 调度器：并行解压多个压缩包
# =====================================================================

class Scheduler:
    def __init__(self, opts: Options, history=None):
        self.opts = opts
        self.history = history
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self.workers: list[threading.Thread] = []
        self.q: queue.Queue = queue.Queue()
        self.on_done = None
        self.running = False

    def start(self, jobs: list[Job]):
        with self.lock:
            for j in jobs:
                if j.state == "等待" and not any(old is j for old in self.jobs):
                    self.jobs.append(j)
                    self.q.put(j)
            self.workers = [t for t in self.workers if t.is_alive()]
            self.running = True
            for _ in range(max(0, max(1, self.opts.parallel_jobs) - len(self.workers))):
                t = threading.Thread(target=self._worker, daemon=True)
                self.workers.append(t)
                t.start()

    def _worker(self):
        while True:
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                # 在锁内注销线程，避免添加新任务时误认为退出中的线程还能接活。
                with self.lock:
                    if self.q.empty():
                        self.workers.remove(threading.current_thread())
                        self.running = bool(self.workers)
                        return
                continue
            if job.state != "等待":
                self.q.task_done()
                continue
            job.state = "解压中"
            from task_history import tracked, item
            def describe(result):
                state = "已完成" if job.state == "完成" else "部分完成" if job.state == "完成(有警告)" else "已取消" if job.state == "已取消" else "失败"
                return [item("\n".join(map(str, job.sources)) if job.mode == "compress" else job.path,
                             (job.path if job.mode == "compress" else job.dest) if job.state.startswith("完成") else "", state, job.error)]
            tracked(self.history, "压缩文件" if job.mode == "compress" else "解压文件", lambda: execute_task(job, self.opts),
                    job.sources if job.mode == "compress" else [job.path], describe, lambda: job.cancel)()
            self.q.task_done()

    def _more_pending(self):
        with self.lock:
            return any(j.state == "等待" for j in self.jobs)

    def cancel_all(self):
        for j in self.jobs:
            if j.state in ("等待", "解压中", "压缩中"):
                j.cancel = True
                if j.state == "等待":
                    j.state = "已取消"


# =====================================================================
# 图形界面
# =====================================================================

def default_config_path():
    """设置文件位置；打包成 exe 后 __file__ 指向临时解包目录，改随 exe 存放。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        probe = os.path.join(base, ".write_probe")
        try:
            with open(probe, "wb"):
                pass
            os.remove(probe)
        except OSError:
            base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "文件工具箱")
            os.makedirs(base, exist_ok=True)
        return os.path.join(base, "archive_settings.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive_settings.json")


def run_gui(parent=None, config_path=None, on_output=None, history=None):
    """创建独立窗口或嵌入页签，并返回任务控制接口。"""
    import json
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
    from types import SimpleNamespace

    CFG = os.fspath(config_path) if config_path is not None else default_config_path()

    def load_cfg():
        try:
            with open(CFG, encoding="utf-8") as f:
                value = json.load(f)
                return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    cfg = load_cfg()

    standalone = parent is None
    root = tk.Tk() if standalone else parent
    if standalone:
        root.title(f"{APP_NAME} SwiftExtract v{APP_VER} — 多格式高速解压")
        root.geometry("1120x660")
        root.minsize(1080, 540)

    try:
        if standalone:
            root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass

    base_font = ("Microsoft YaHei UI", 10)

    # ---------- 顶栏 ----------
    top = ttk.Frame(root, padding=(12, 8, 12, 4))
    top.pack(fill="x")
    ttk.Label(top, text="⚡ 疾速解压", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
    engine_label = ttk.Label(top, foreground="#666")
    engine_label.pack(side="left", padx=8)

    def refresh_engines():
        badges = [f"分流引擎×{auto_workers()}"]
        if runtime_manager.get_tool("7zip", discover=False):
            badges.append("7-Zip✓（7z / RAR / AES）")
        if WINRAR or UNRAR:
            badges.append("WinRAR / UnRAR✓")
        if py7zr:
            badges.append("py7zr✓")
        if pyzipper:
            badges.append("AES✓")
        if zstandard or runtime_manager.get_tool("zstd", discover=False):
            badges.append("Zstd✓")
        engine_label.configure(text="   引擎: " + " | ".join(badges))

    refresh_engines()

    # ---------- 工具行 ----------
    bar = ttk.Frame(root, padding=(12, 2, 12, 4))
    bar.pack(fill="x")
    ttk.Button(bar, text="＋ 添加压缩包", command=lambda: add_files()).pack(side="left")
    ttk.Button(bar, text="移除所选", command=lambda: remove_selected()).pack(side="left", padx=6)
    ttk.Button(bar, text="清空已完成", command=lambda: clear_finished()).pack(side="left")
    ttk.Button(bar, text="📦 打包压缩…", command=lambda: open_compress_dialog()).pack(side="right")

    # ---------- 设置行 ----------
    sbar = ttk.Frame(root, padding=(12, 4, 12, 4))
    sbar.pack(fill="x")

    outvar = tk.StringVar(root, value=cfg.get("outdir", ""))
    use_sub = tk.BooleanVar(root, value=cfg.get("subfolder", True))
    split_var = tk.BooleanVar(root, value=cfg.get("big_split", True))
    gbk_var = tk.BooleanVar(root, value=cfg.get("gbk_fix", True))
    workers_var = tk.IntVar(root, value=cfg.get("workers", 0) or 0)
    pjobs_var = tk.IntVar(root, value=cfg.get("parallel_jobs", 2))

    ttk.Label(sbar, text="输出目录:").pack(side="left")
    e = ttk.Entry(sbar, textvariable=outvar, width=44)
    e.pack(side="left", padx=4)
    ttk.Button(sbar, text="浏览…", command=lambda: browse_out()).pack(side="left")
    ttk.Label(sbar, text="(留空=压缩包所在目录)").pack(side="left", padx=(2, 10))

    ttk.Checkbutton(sbar, text="解压到子文件夹", variable=use_sub).pack(side="left")
    ttk.Checkbutton(sbar, text="大文件分流", variable=split_var).pack(side="left", padx=(8, 0))
    ttk.Checkbutton(sbar, text="中文GBK修复", variable=gbk_var).pack(side="left", padx=(8, 0))

    sbar2 = ttk.Frame(root, padding=(12, 0, 12, 4))
    sbar2.pack(fill="x")
    ttk.Label(sbar2, text="单包并行线程:").pack(side="left")
    wcombo = ttk.Combobox(sbar2, textvariable=workers_var, width=6, state="readonly",
                          values=[0, 2, 4, 6, 8, 12, 16])
    wcombo.pack(side="left", padx=(4, 2))
    ttk.Label(sbar2, text="(0=自动)").pack(side="left")
    ttk.Label(sbar2, text="    同时解压任务数:").pack(side="left")
    pcombo = ttk.Combobox(sbar2, textvariable=pjobs_var, width=4, state="readonly",
                          values=[1, 2, 3, 4, 6])
    pcombo.pack(side="left", padx=(4, 0))

    # ---------- 文件列表 ----------
    mid = ttk.Frame(root, padding=(12, 4, 12, 4))
    mid.pack(fill="both", expand=True)
    cols = ("fmt", "size", "progress", "speed", "state")
    tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="extended")
    tree.heading("#0", text="压缩包", anchor="w")
    tree.heading("fmt", text="格式")
    tree.heading("size", text="大小")
    tree.heading("progress", text="进度")
    tree.heading("speed", text="速度")
    tree.heading("state", text="状态")
    tree.column("#0", width=380, minwidth=220)
    tree.column("fmt", width=56, anchor="center")
    tree.column("size", width=86, anchor="e")
    tree.column("progress", width=170, anchor="w")
    tree.column("speed", width=100, anchor="e")
    tree.column("state", width=90, anchor="center")
    vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="left", fill="y")

    tree.tag_configure("done", foreground="#0a7d32")
    tree.tag_configure("fail", foreground="#c0392b")
    tree.tag_configure("run", foreground="#1a56c4")

    # ---------- 底部：总进度 + 按钮 + 日志 ----------
    bot = ttk.Frame(root, padding=(12, 4, 12, 8))
    bot.pack(fill="x")
    overall = ttk.Progressbar(bot, mode="determinate", length=320)
    overall.pack(side="left")
    total_lbl = tk.Label(bot, text="就绪", font=base_font, fg="#444")
    total_lbl.pack(side="left", padx=10)
    go_btn = ttk.Button(bot, text="▶  开始解压", command=lambda: start_all())
    go_btn.pack(side="right")
    stop_btn = ttk.Button(bot, text="■ 停止", command=lambda: stop_all(), state="disabled")
    stop_btn.pack(side="right", padx=6)
    open_btn = ttk.Button(bot, text="打开输出目录", command=lambda: open_dest())
    open_btn.pack(side="right", padx=6)

    logbox = tk.Text(root, height=6, font=("Consolas", 9), state="disabled",
                     bg="#1e1e1e", fg="#d4d4d4", relief="flat")
    logbox.pack(fill="x", padx=12, pady=(0, 8))

    def log(msg):
        logbox.configure(state="normal")
        logbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    # ---------- 状态 ----------
    jobs: list[Job] = []
    row_map: dict[str, str] = {}
    sched = [None]
    closing = [False]
    refresh_timer = [None]
    password_requests = queue.Queue()

    def current_opts() -> Options:
        return Options(
            workers=int(workers_var.get()),
            parallel_jobs=int(pjobs_var.get()),
            big_split=bool(split_var.get()),
            gbk_fix=bool(gbk_var.get()),
            subfolder=bool(use_sub.get()),
            outdir=outvar.get().strip(),
        )

    def dest_for(path: str, opts: Options) -> str:
        base = opts.outdir or os.path.dirname(path)
        if opts.subfolder:
            stem = archive_stem(path)
            return os.path.join(base, stem or "extracted")
        return base

    def add_paths(paths):
        opts = current_opts()
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if not os.path.isfile(p):
                continue
            canonical, group = volume_base(p)
            if any(j.path == canonical for j in jobs):
                continue
            parts = all_volume_parts(canonical, group)
            kind = "split" if len(parts) > 1 else detect_format(canonical)
            job = Job(path=canonical, kind=kind)
            job.dest = dest_for(canonical, opts)
            jobs.append(job)
            add_row(job)
            added += 1
        if added:
            log(f"已添加 {added} 个压缩包")
            save_cfg()

    def add_row(job: Job):
        if job.mode == "compress":
            iid = tree.insert("", "end", text="📦 " + os.path.basename(job.path), values=(
                job.kind or "zip", fmt_size(job.bytes_total), "—", "", job.state))
            row_map[job.path] = iid
            return
        name = os.path.basename(job.path)
        if volume_base(job.path)[1]:
            n = len(all_volume_parts(*volume_base(job.path)))
            name += f"  (共{n}卷)"
        iid = tree.insert("", "end", text=name, values=(
            job.kind or "?", fmt_size(os.path.getsize(job.path)), "—", "", job.state))
        row_map[job.path] = iid

    def add_compress_job(job: Job):
        if any(j.path == job.path and j.mode == "compress" for j in jobs):
            messagebox.showwarning(APP_NAME, "该输出档案已在任务列表中", parent=root)
            return
        jobs.append(job)
        add_row(job)
        log(f"添加压缩任务 → {job.path} ({job.kind}, {LEVELS_ZH[job.level]})")
        if sched[0] is None:
            sched[0] = Scheduler(current_opts(), history)
        sched[0].opts = current_opts()
        sched[0].start([job])
        go_btn.configure(state="disabled")
        stop_btn.configure(state="normal")

    def open_compress_dialog():
        win = tk.Toplevel(root)
        win.title("打包压缩")
        win.geometry("680x460")
        win.transient(root.winfo_toplevel())
        ttk.Label(win, text="📦 打包压缩（支持 zip / 7z / tar 系）",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=12, pady=(10, 4))

        srcs = []
        lb = tk.Listbox(win, height=8)
        lb.pack(fill="both", expand=True, padx=12, pady=4)

        def redraw():
            lb.delete(0, "end")
            for s in srcs:
                lb.insert("end", s)

        def add_f():
            for f in filedialog.askopenfilenames(parent=win, title="选择文件（可多选）"):
                if f not in srcs:
                    srcs.append(f)
            redraw()

        def add_d():
            d = filedialog.askdirectory(parent=win, title="选择文件夹")
            if d and d not in srcs:
                srcs.append(os.path.normpath(d))
            redraw()

        def rm_sel():
            for i in reversed(lb.curselection()):
                srcs.pop(i)
            redraw()

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12)
        ttk.Button(btns, text="＋ 添加文件", command=add_f).pack(side="left")
        ttk.Button(btns, text="＋ 添加文件夹", command=add_d).pack(side="left", padx=6)
        ttk.Button(btns, text="移除所选", command=rm_sel).pack(side="left")

        opts_f = ttk.Frame(win)
        opts_f.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Label(opts_f, text="输出档案:").pack(side="left")
        out_var = tk.StringVar(root)
        ttk.Entry(opts_f, textvariable=out_var, width=46).pack(side="left", padx=4)

        fmt_var = tk.StringVar(root, value="zip")
        lvl_var = tk.StringVar(root, value="normal")
        pwd_var = tk.StringVar(root)

        def update_default_out(*_):
            if out_var.get() and fmt_from_ext(out_var.get()):
                return  # 用户已手填
            if not srcs:
                return
            base_dir = os.path.dirname(srcs[0])
            name = os.path.basename(srcs[0].rstrip("\\/")) or "archive"
            ext = {"zip": ".zip", "7z": ".7z", "tar": ".tar", "tar.gz": ".tar.gz",
                   "tar.xz": ".tar.xz", "tar.zst": ".tar.zst"}[fmt_var.get()]
            out_var.set(os.path.join(base_dir, name + ext))

        fmt_f = ttk.Frame(win)
        fmt_f.pack(fill="x", padx=12, pady=2)
        ttk.Label(fmt_f, text="格式:").pack(side="left")
        cb = ttk.Combobox(fmt_f, textvariable=fmt_var, width=8, state="readonly",
                          values=["zip", "7z", "tar", "tar.gz", "tar.xz", "tar.zst"])
        cb.pack(side="left", padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", update_default_out)
        ttk.Label(fmt_f, text="级别:").pack(side="left")
        ttk.Combobox(fmt_f, textvariable=lvl_var, width=8, state="readonly",
                     values=["store", "fast", "normal", "max"]).pack(side="left", padx=(4, 12))
        ttk.Label(fmt_f, text="(store=不压缩, max=最小体积)").pack(side="left")
        ttk.Label(fmt_f, text="密码:").pack(side="left")
        ttk.Entry(fmt_f, textvariable=pwd_var, width=16, show="●").pack(side="left", padx=(4, 0))
        ttk.Label(fmt_f, text="(zip=AES, 7z=AES)").pack(side="left")

        out_var.trace_add("write", lambda *a: None)
        win.bind("<Map>", lambda e: update_default_out() if srcs else None)

        def do_start():
            if not srcs:
                messagebox.showwarning(APP_NAME, "请先添加要压缩的文件/文件夹", parent=win)
                return
            outp = out_var.get().strip()
            if not outp:
                update_default_out()
                outp = out_var.get().strip()
            if os.path.exists(outp):
                if not messagebox.askyesno(APP_NAME, f"{outp}\n已存在，覆盖？", parent=win):
                    return
            job = Job(path=os.path.abspath(outp), mode="compress", sources=list(srcs),
                      kind=fmt_var.get(), level=lvl_var.get(), password=pwd_var.get(),
                      dest=os.path.dirname(os.path.abspath(outp)))
            job.bytes_total = sum(
                (os.path.getsize(s) if os.path.isfile(s) else
                 sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(s) for f in fs))
                for s in srcs)
            win.destroy()
            add_compress_job(job)

        ttk.Button(win, text="▶ 开始压缩", command=do_start).pack(pady=10)

    def add_files():
        files = filedialog.askopenfilenames(
            parent=root, title="选择压缩包（可多选）",
            filetypes=[("所有支持的压缩包",
                        "*.zip *.7z *.rar *.tar *.gz *.bz2 *.xz *.zst *.tgz *.tbz2 *.txz "
                        "*.cab *.iso *.arj *.lzh *.jar *.lz4 *.001 *.z01 *.part1.rar *.whl *.apk *.exe"),
                       ("所有文件", "*.*")])
        if files:
            add_paths(files)

    def browse_out():
        d = filedialog.askdirectory(parent=root, title="选择输出目录")
        if d:
            outvar.set(os.path.normpath(d))
            save_cfg()

    def remove_selected():
        for iid in tree.selection():
            for j in [j for j in jobs if row_map.get(j.path) == iid]:
                if j.state in ("解压中", "压缩中"):
                    continue
                jobs.remove(j)
                row_map.pop(j.path, None)
                tree.delete(iid)

    def clear_finished():
        for j in [j for j in jobs if j.state in ("完成", "完成(有警告)", "失败", "已取消", "需要密码")]:
            iid = row_map.pop(j.path, None)
            if iid:
                tree.delete(iid)
            jobs.remove(j)

    def save_cfg():
        try:
            with open(CFG, "w", encoding="utf-8") as f:
                json.dump({"outdir": outvar.get(), "subfolder": use_sub.get(),
                           "big_split": split_var.get(), "gbk_fix": gbk_var.get(),
                           "workers": int(workers_var.get()), "parallel_jobs": int(pjobs_var.get())},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def ask_password_gui(job: Job):
        ev = threading.Event()
        holder = {}
        password_requests.put((job, ev, holder))
        deadline = time.monotonic() + 600
        while not ev.wait(0.1):
            if closing[0] or job.cancel or time.monotonic() >= deadline:
                holder["expired"] = True
                return None
        return holder.get("pwd")

    def start_all():
        pending = [j for j in jobs if j.mode == "extract" and j.state in ("等待", "需要密码")]
        if not pending:
            messagebox.showinfo(APP_NAME, "没有待解压的任务，请先添加压缩包。", parent=root)
            return
        opts = current_opts()
        for j in pending:
            j.dest = dest_for(j.path, opts)
            j.state = "等待"
            j.cancel = False
            j.error = ""
        if sched[0] is None or not sched[0].running:
            sched[0] = Scheduler(opts, history)
        else:
            sched[0].opts = opts
        go_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        log(f"开始解压 {len(pending)} 个任务 (并行线程={opts.workers or '自动'}, 分流={'开' if opts.big_split else '关'})")
        sched[0].start(pending)

    def stop_all():
        if sched[0]:
            sched[0].cancel_all()
            log("正在停止…")

    def busy():
        return bool(sched[0] and any(t.is_alive() for t in sched[0].workers))

    def shutdown():
        closing[0] = True
        stop_all()
        save_cfg()
        if refresh_timer[0] is not None:
            root.after_cancel(refresh_timer[0])
            refresh_timer[0] = None
        global _pwd_prompt
        if _pwd_prompt is ask_password_gui:
            _pwd_prompt = None

    def selected_outputs():
        selected = set(tree.selection())
        return list(dict.fromkeys(
            j.dest for j in jobs
            if j.mode == "extract" and j.state == "完成" and not j.error
            and os.path.isdir(j.dest)
            and (not selected or row_map.get(j.path) in selected)
        ))

    def send_output():
        paths = selected_outputs()
        if not paths:
            messagebox.showinfo(APP_NAME, "请先完成解压，或选中已成功解压的任务。", parent=root)
            return
        on_output(paths)

    if on_output is not None:
        ttk.Button(bar, text="解压结果 → 转码", command=send_output).pack(side="left", padx=10)

    def open_dest():
        sel = tree.selection()
        for iid in sel:
            for j in jobs:
                if row_map.get(j.path) == iid:
                    os.makedirs(j.dest, exist_ok=True)
                    os.startfile(j.dest)  # noqa
                    return
        if jobs:
            os.makedirs(jobs[-1].dest, exist_ok=True)
            os.startfile(jobs[-1].dest)  # noqa

    def refresh():
        if closing[0]:
            return
        while not password_requests.empty():
            job, ev, holder = password_requests.get_nowait()
            try:
                if not holder.get("expired") and not job.cancel:
                    holder["pwd"] = simpledialog.askstring(
                        "需要密码", f"{os.path.basename(job.path)} 已加密，请输入密码：",
                        parent=root, show="●")
            finally:
                ev.set()
        now = time.perf_counter()
        total_b = done_b = 0
        active = 0
        agg_speed = 0.0
        for j in jobs:
            iid = row_map.get(j.path)
            if not iid:
                continue
            if j.state in ("解压中", "压缩中"):
                j.tick()
                active += 1
                agg_speed += j.speed
            prog_txt = "—"
            if j.bytes_total:
                pct = min(100, j.bytes_done * 100 // j.bytes_total)
                bar_txt = "▓" * (pct // 5) + "░" * (20 - pct // 5)
                prog_txt = f"{bar_txt} {pct}%"
            elif j.files_total:
                prog_txt = f"{j.files_done}/{j.files_total} 项"
            speed_txt = fmt_size(j.speed) + "/s" if j.speed > 1 and j.state in ("解压中", "压缩中") else (
                fmt_size(j.bytes_total / j.elapsed) + "/s" if j.state in ("完成", "完成(有警告)") and j.elapsed > 0 and j.bytes_total else "")
            state_txt = j.state
            if j.error and j.state not in ("解压中", "压缩中"):
                state_txt += " ⚠"
            tags = ()
            if j.state in ("完成", "完成(有警告)"):
                tags = ("done",)
            elif j.state in ("失败", "已取消", "需要密码"):
                tags = ("fail",)
            elif j.state in ("解压中", "压缩中"):
                tags = ("run",)
            try:
                tree.item(iid, values=(j.kind or "?",
                                       tree.set(iid, "size"),
                                       prog_txt, speed_txt, state_txt), tags=tags)
            except tk.TclError:
                pass
            if j.bytes_total:
                total_b += j.bytes_total
                done_b += min(j.bytes_done, j.bytes_total)
        if active:
            total_lbl.configure(
                text=f"处理中… {active} 个任务 | 总进度 {done_b * 100 // total_b if total_b else 0}% "
                     f"({fmt_size(done_b)}/{fmt_size(total_b)}) | 总速度 {fmt_size(agg_speed)}/s",
                fg="#1a56c4")
        else:
            finished = [j for j in jobs if j.state in ("完成", "完成(有警告)", "失败", "已取消", "需要密码")]
            if finished:
                ok = sum(1 for j in finished if j.state.startswith("完成"))
                total_lbl.configure(
                    text=f"完成 {ok}/{len(finished)} 个任务" +
                         (f"，{sum(1 for j in finished if j.state == '失败')} 个失败" if any(j.state == "失败" for j in finished) else ""),
                    fg="#0a7d32")
                if go_btn.instate(["disabled"]):
                    go_btn.configure(state="normal")
                    stop_btn.configure(state="disabled")
                    try:
                        import winsound
                        winsound.MessageBeep()
                    except Exception:
                        pass
        refresh_timer[0] = root.after(200, refresh)

    def on_double(_event):
        for iid in tree.selection():
            for j in jobs:
                if row_map.get(j.path) != iid:
                    continue
                if j.mode == "compress":
                    d = os.path.dirname(j.path)
                    if os.path.isdir(d):
                        os.startfile(d)  # noqa
                elif os.path.isdir(j.dest):
                    os.startfile(j.dest)  # noqa

    tree.bind("<Double-1>", on_double)

    # 拖拽支持
    if windnd:
        try:
            windnd.hook_dropfiles(root, func=lambda files: add_paths([f.decode("gbk") if isinstance(f, bytes) else f for f in files]))
        except Exception:
            pass

    global _pwd_prompt
    _pwd_prompt = ask_password_gui

    log("就绪。解压：添加压缩包→开始解压；压缩：右上「📦 打包压缩…」。支持 zip/7z/tar系/gz/xz/zst/rar/分卷…")
    refresh_timer[0] = root.after(200, refresh)
    controller = SimpleNamespace(
        jobs=jobs, tree=tree, out_dir_var=outvar, subfolder_var=use_sub,
        add_paths=add_paths, start_all=start_all, add_compress_job=add_compress_job,
        open_compress_dialog=open_compress_dialog, selected_outputs=selected_outputs,
        send_output=send_output, busy=busy, stop_all=stop_all, shutdown=shutdown,
        refresh_engines=refresh_engines,
    )
    if standalone:
        def close_window():
            shutdown()
            def finish_close():
                if busy():
                    root.after(100, finish_close)
                else:
                    root.destroy()
            finish_close()
        root.protocol("WM_DELETE_WINDOW", close_window)
        root.mainloop()
    return controller


# =====================================================================
# 命令行模式
# =====================================================================


def run_cli(argv):
    import argparse
    try:  # 管道/重定向时输出统一为 UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="swift_extract", description="疾速解压 — 多格式高速解压/压缩")
    ap.add_argument("files", nargs="+", help="解压: 压缩包路径 / 压缩(-c): 文件或目录")
    ap.add_argument("-c", "--compress", action="store_true", help="压缩模式：把 files 打包为 -o 指定的档案")
    ap.add_argument("-o", "--outdir", default="", help="解压: 输出目录 / 压缩: 输出档案路径（按扩展名定格式）")
    ap.add_argument("-F", "--format", dest="fmt", default="",
                    choices=["", "zip", "7z", "tar", "tar.gz", "tar.xz", "tar.zst"],
                    help="压缩格式（默认按 -o 扩展名推断，无 -o 时为 zip）")
    ap.add_argument("-L", "--level", default="normal",
                    choices=["store", "fast", "normal", "max"], help="压缩级别")
    ap.add_argument("-j", "--workers", type=int, default=0, help="并行线程数（0=自动）")
    ap.add_argument("--flat", action="store_true", help="解压不建子文件夹")
    ap.add_argument("-p", "--password", default="", help="密码（解压加密包 / 加密压缩 zip·7z）")
    ap.add_argument("--no-split", action="store_true", help="关闭大文件分流")
    ap.add_argument("--no-gbk", action="store_true", help="关闭中文文件名 GBK 修复")
    args = ap.parse_args(argv)

    opts = Options(workers=args.workers, parallel_jobs=2,
                   big_split=not args.no_split, gbk_fix=not args.no_gbk,
                   subfolder=not args.flat, outdir=args.outdir, password=args.password)

    if args.compress:
        return _cli_compress(args, opts)
    return _cli_extract(args, opts)


def _cli_compress(args, opts):
    import time as _t
    out = args.outdir
    if not out:
        first = os.path.abspath(args.files[0])
        stem = os.path.basename(first.rstrip("\\/"))
        out = os.path.join(os.path.dirname(first), stem + ".zip")
    fmt = args.fmt or fmt_from_ext(out)
    if not fmt:
        out += ".zip"
        fmt = "zip"
    for s in args.files:
        if not os.path.exists(s):
            print(f"[跳过] 不存在: {s}")
            args.files.remove(s)
    if not args.files:
        print("没有可压缩的源")
        return 1
    job = Job(path=out, mode="compress", sources=list(args.files),
              kind=fmt, level=args.level, password=args.password,
              dest=os.path.dirname(out) or ".")
    job.kind = fmt
    total = sum((os.path.getsize(s) if os.path.isfile(s) else
                 sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(s) for f in fs))
                for s in args.files)
    job.bytes_total = total
    print(f"[压缩] {len(args.files)} 个源 ({fmt_size(total)}) → {out}  "
          f"格式={fmt} 级别={LEVELS_ZH[args.level]}"
          + (" [AES加密]" if args.password and fmt in ("zip", "7z") else ""))
    t0 = _t.perf_counter()
    execute_task(job, opts)
    if job.state in ("失败", "需要密码", "已取消"):
        print(f"  ✗ {job.state}: {job.error}")
        return 1
    dur = _t.perf_counter() - t0
    out_size = os.path.getsize(out) if os.path.isfile(out) else 0
    print(f"  ✓ 完成  {job.files_done} 项  {fmt_size(total)} → {fmt_size(out_size)} "
          f"({out_size * 100 // total if total else 100}%)  用时 {dur:.2f}s  "
          f"平均 {fmt_size(total / dur) if dur else 0}/s")
    return 0


def _cli_extract(args, opts):
    jobs = []
    for p in args.files:
        if not os.path.isfile(p):
            print(f"[跳过] 文件不存在: {p}")
            continue
        canonical, group = volume_base(p)
        parts = all_volume_parts(canonical, group)
        kind = "split" if len(parts) > 1 else detect_format(canonical)
        if opts.outdir:
            base = opts.outdir
        else:
            base = os.path.dirname(canonical)
        dest = os.path.join(base, archive_stem(canonical) or "extracted") if opts.subfolder else base
        jobs.append(Job(path=canonical, kind=kind, dest=dest))
        print(f"[+] {canonical}  格式={kind}  分卷={len(parts)}  → {dest}")

    fails = 0
    for job in jobs:
        print(f"\n=== 解压 {os.path.basename(job.path)} ===")
        execute_task(job, opts)
        if job.state == "需要密码":
            print("  ✗ 需要密码（用 -p 传入）")
            fails += 1
        elif job.state == "失败":
            print(f"  ✗ 失败: {job.error}")
            fails += 1
        elif job.state == "已取消":
            print("  - 已取消")
        else:
            dur = job.elapsed
            avg = job.bytes_total / dur if job.bytes_total and dur else 0
            print(f"  ✓ {job.state}  {job.files_done}/{job.files_total} 项  "
                  f"{fmt_size(job.bytes_done)}  用时 {dur:.2f}s  平均 {fmt_size(avg)}/s"
                  + (f"  ⚠ {job.error}" if job.error else ""))
    print(f"\n完成: {len(jobs) - fails}/{len(jobs)}")
    return 1 if fails else 0


# =====================================================================

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(run_cli(sys.argv[2:]))
    run_gui()


if __name__ == "__main__":
    main()
