#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 盘清理：扫描可以安全删除的垃圾文件和隐私痕迹，勾选后清理。

纯标准库实现，分两级：
  * 用户级：不需要管理员权限（临时文件、浏览器缓存、缩略图、最近使用记录、错误报告等）；
  * 管理员级：需要 UAC 授权，由工具箱用独立的管理员进程执行
    （系统临时文件、Windows 更新缓存、Prefetch、事件日志、DNS 缓存等）。

安全设计：
  * 只清理位置固定、可再生成的目录，绝不按外部输入的任意路径删东西
    （管理员任务文件里只允许出现本模块预定义的项目 id，提权执行时忽略一切路径覆盖）；
  * 先扫描列出大小，用户勾选并确认后再动手；被占用的文件一律跳过并计数；
  * 遍历时跳过所有联接点/符号链接，避免循环目录；
  * 不做“覆写空闲空间”之类的防恢复擦除——SSD 时代收益很小，还会损伤闪存寿命。
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

NO_WINDOW = 0x08000000        # CREATE_NO_WINDOW，不闪控制台窗口
ADMIN_TASK_FLAG = "--admin-task"
RECYCLE_ROOT = "C:\\"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

ADMIN_ITEM_IDS = frozenset({
    "windows_temp", "update_cache", "prefetch", "wer_admin", "event_logs", "dns_cache",
})

CHROMIUM_CACHE_SUBDIRS = (
    "Cache", "Code Cache", "GPUCache", "Media Cache",
    os.path.join("Service Worker", "CacheStorage"),
    os.path.join("Service Worker", "ScriptCache"),
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _reg():
    if os.name != "nt":
        raise OSError("注册表操作仅适用于 Windows。")
    import winreg
    return winreg


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _roaming_appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def _windows_root() -> Path:
    return Path(os.environ.get("SystemRoot") or r"C:\Windows")


def _program_data() -> Path:
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData")


def _subprocess_flags() -> int:
    return NO_WINDOW if os.name == "nt" else 0


def _walk_files(root: Path, min_age_seconds: float = 0.0, now: float | None = None):
    """遍历目录产出 (路径, 大小, 修改时间)；不进入任何联接点/符号链接。

    注意：部分 Python 3.14 构建里 DirEntry.st_file_attributes 在 stat()
    之前不填充，所以必须显式 stat(follow_symlinks=False) 再看属性。
    """
    now = time.time() if now is None else now
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
                attrs = getattr(info, "st_file_attributes", 0)
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if stat_module.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                elif stat_module.S_ISREG(info.st_mode):
                    if now - info.st_mtime < min_age_seconds:
                        continue
                    yield entry.path, info.st_size, info.st_mtime
            except OSError:
                continue


def measure_dir(root: Path, min_age_seconds: float = 0.0) -> tuple[int, int]:
    """返回 (总字节, 文件数)。"""
    total_bytes = total_files = 0
    for _path, size, _mtime in _walk_files(root, min_age_seconds):
        total_bytes += size
        total_files += 1
    return total_bytes, total_files


def clear_dir(root: Path, min_age_seconds: float = 0.0) -> tuple[int, int, int]:
    """清空目录内容并尽量删掉空子目录，返回 (释放字节, 删除文件数, 跳过数)。"""
    now = time.time()
    freed = deleted = skipped = 0
    dirs: list[str] = []
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
                attrs = getattr(info, "st_file_attributes", 0)
                if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if stat_module.S_ISDIR(info.st_mode):
                    dirs.append(entry.path)
                    stack.append(entry.path)
                    continue
                if not stat_module.S_ISREG(info.st_mode):
                    continue
                if now - info.st_mtime < min_age_seconds:
                    continue
                os.unlink(entry.path)
                freed += info.st_size
                deleted += 1
            except OSError:
                skipped += 1
    for path in sorted(dirs, key=len, reverse=True):
        try:
            os.rmdir(path)
        except OSError:
            pass
    return freed, deleted, skipped


def fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


# ---------------------------------------------------------------------------
# 注册表清理（最近使用记录）
# ---------------------------------------------------------------------------

def registry_entry_count(path: str) -> int:
    """统计键下全部值（含子键）的数量；键不存在返回 0。"""
    winreg = _reg()

    def count(key_path: str) -> int:
        total = 0
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                subkeys, values, _kind = winreg.QueryInfoKey(key)
                total += values
                for i in range(subkeys):
                    total += count(key_path + "\\" + winreg.EnumKey(key, i))
        except OSError:
            pass
        return total

    return count(path)


def registry_delete_tree(path: str) -> int:
    """删除整个键（含子键和值），返回清除的值数量；键不存在返回 0。"""
    winreg = _reg()
    cleared = 0

    def purge(key_path: str) -> None:
        nonlocal cleared
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
                subkeys, values, _kind = winreg.QueryInfoKey(key)
                cleared += values
                for i in range(subkeys):
                    purge(key_path + "\\" + winreg.EnumKey(key, i))
                for _ in range(values):
                    try:
                        winreg.DeleteValue(key, winreg.EnumValue(key, 0)[0])
                    except OSError:
                        break
        except OSError:
            return
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            pass

    purge(path)
    return cleared


def _recent_registry_paths(override: str | None = None) -> list[str]:
    base = override or r"Software\Microsoft\Windows\CurrentVersion\Explorer"
    return [
        base + r"\RecentDocs",
        base + r"\TypedPaths",
        base + r"\RunMRU",
        base + r"\ComDlg32\OpenSavePidlMRU",
    ]


# ---------------------------------------------------------------------------
# 浏览器缓存位置
# ---------------------------------------------------------------------------

def browser_cache_roots(local_root: Path | None = None) -> list[Path]:
    """收集 Edge / Chrome / Firefox 的缓存目录；只返回真实存在的。"""
    local = Path(local_root) if local_root is not None else _local_appdata()
    roots: list[Path] = []
    for vendor in (r"Microsoft\Edge", r"Google\Chrome"):
        user_data = local / vendor / "User Data"
        if not user_data.is_dir():
            continue
        try:
            profiles = list(user_data.iterdir())
        except OSError:
            continue
        for profile in profiles:
            if profile.name in ("Default", "Guest Profile") or profile.name.startswith("Profile "):
                for sub in CHROMIUM_CACHE_SUBDIRS:
                    cache = profile / sub
                    if cache.is_dir():
                        roots.append(cache)
    firefox_profiles = local / "Mozilla" / "Firefox" / "Profiles"
    if firefox_profiles.is_dir():
        try:
            for profile in firefox_profiles.iterdir():
                cache = profile / "cache2"
                if cache.is_dir():
                    roots.append(cache)
        except OSError:
            pass
    return roots


# ---------------------------------------------------------------------------
# 回收站 / 事件日志 / DNS
# ---------------------------------------------------------------------------

def recycle_bin_info(root_path: str = RECYCLE_ROOT) -> tuple[int, int]:
    """返回回收站 (占用字节, 项目数)；仅 Windows。"""
    if os.name != "nt":
        raise OSError("回收站操作仅适用于 Windows。")
    import ctypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong),
                    ("i64Size", ctypes.c_int64),
                    ("i64NumItems", ctypes.c_int64)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    hr = shell.SHQueryRecycleBinW(str(root_path), ctypes.byref(info))
    if hr != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def empty_recycle_bin(root_path: str = RECYCLE_ROOT) -> bool:
    if os.name != "nt":
        raise OSError("回收站操作仅适用于 Windows。")
    import ctypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    return shell.SHEmptyRecycleBinW(None, str(root_path), 0x0007) == 0


def event_log_channels() -> list[str]:
    raw = subprocess.run(["wevtutil", "el"], capture_output=True, timeout=120,
                         creationflags=_subprocess_flags())
    if raw.returncode != 0:
        raise OSError(raw.stderr.decode(errors="replace").strip() or "wevtutil 列举日志失败")
    return [line.strip() for line in raw.stdout.decode(errors="replace").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 清理项定义
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ItemScan:
    bytes: int = 0
    files: int = 0
    records: int = 0
    note: str = ""


@dataclasses.dataclass
class ItemResult:
    item: str = ""
    bytes_freed: int = 0
    files_deleted: int = 0
    skipped: int = 0
    records_cleared: int = 0
    note: str = ""
    ok: bool = True
    error: str = ""


@dataclasses.dataclass(frozen=True)
class ItemDef:
    id: str
    name: str
    scope: str            # "user" | "admin"
    default_on: bool
    description: str
    scan: object          # Callable[[CleanEngine], ItemScan]
    clean: object         # Callable[[CleanEngine, ItemResult], None]


def _scan_contents(en: "CleanEngine", item_id: str, defaults, min_age: bool = False) -> ItemScan:
    total_bytes = total_files = 0
    for root in en.roots(item_id, defaults):
        if not Path(root).is_dir():
            continue
        size, files = measure_dir(Path(root), en.min_age_seconds if min_age else 0.0)
        total_bytes += size
        total_files += files
    return ItemScan(bytes=total_bytes, files=total_files)


def _clean_contents(en: "CleanEngine", item_id: str, defaults, result: ItemResult,
                    min_age: bool = False) -> None:
    for root in en.roots(item_id, defaults):
        if not Path(root).is_dir():
            continue
        freed, deleted, skipped = clear_dir(Path(root), en.min_age_seconds if min_age else 0.0)
        result.bytes_freed += freed
        result.files_deleted += deleted
        result.skipped += skipped
    if result.skipped:
        result.note = f"{result.skipped} 个文件被占用，已跳过"


def _thumbnail_files(en: "CleanEngine") -> list[tuple[Path, int]]:
    dirs = en.roots("thumbnails", lambda: [_local_appdata() / "Microsoft" / "Windows" / "Explorer"])
    patterns = ("thumbcache_*.db", "iconcache_*.db")
    out: list[tuple[Path, int]] = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for pattern in patterns:
            for f in d.glob(pattern):
                try:
                    out.append((f, f.stat().st_size))
                except OSError:
                    continue
    if "thumbnails" not in en.root_overrides:
        legacy = _local_appdata() / "iconcache.db"   # 旧版图标缓存位置
        if legacy.is_file():
            try:
                out.append((legacy, legacy.stat().st_size))
            except OSError:
                pass
    return out


def _wer_user_dirs():
    wer = _local_appdata() / "Microsoft" / "Windows" / "WER"
    return [wer / "ReportArchive", wer / "ReportQueue", wer / "Temp"]


def _wer_admin_dirs():
    wer = _program_data() / "Microsoft" / "Windows" / "WER"
    return [wer / "ReportArchive", wer / "ReportQueue", wer / "Temp"]


# ---- 各项目的扫描 / 清理实现 ----

def _scan_user_temp(en):
    return _scan_contents(en, "user_temp", lambda: [Path(tempfile.gettempdir())], min_age=True)


def _clean_user_temp(en, result):
    _clean_contents(en, "user_temp", lambda: [Path(tempfile.gettempdir())], result, min_age=True)


def _scan_browser_cache(en):
    return _scan_contents(en, "browser_cache", lambda: browser_cache_roots())


def _clean_browser_cache(en, result):
    _clean_contents(en, "browser_cache", lambda: browser_cache_roots(), result)


def _scan_thumbnails(en):
    files = _thumbnail_files(en)
    return ItemScan(bytes=sum(s for _p, s in files), files=len(files))


def _clean_thumbnails(en, result):
    for path, size in _thumbnail_files(en):
        try:
            os.unlink(path)
            result.bytes_freed += size
            result.files_deleted += 1
        except OSError:
            result.skipped += 1
    if result.skipped:
        result.note = f"{result.skipped} 个缓存正被资源管理器占用，已跳过"


def _recent_dir(en) -> Path:
    dirs = en.roots("recent_docs", lambda: [_roaming_appdata() / "Microsoft" / "Windows" / "Recent"])
    return Path(dirs[0])


def _scan_recent(en):
    scan = ItemScan()
    recent = _recent_dir(en)
    if recent.is_dir():
        scan.bytes, scan.files = measure_dir(recent)
    scan.records = sum(registry_entry_count(p) for p in
                       _recent_registry_paths(en.registry_overrides.get("recent_docs")))
    return scan


def _clean_recent(en, result):
    recent = _recent_dir(en)
    if recent.is_dir():
        freed, deleted, skipped = clear_dir(recent)
        result.bytes_freed += freed
        result.files_deleted += deleted
        result.skipped += skipped
    for path in _recent_registry_paths(en.registry_overrides.get("recent_docs")):
        result.records_cleared += registry_delete_tree(path)


def _scan_recycle(en):
    size, items = recycle_bin_info()
    note = "清空后无法恢复，请确认没有要找回的文件" if items else "回收站是空的"
    return ItemScan(bytes=size, records=items, note=note)


def _clean_recycle(en, result):
    size, items = recycle_bin_info()
    if items == 0:
        result.note = "回收站本来就是空的"
        return
    if not empty_recycle_bin():
        raise OSError("清空回收站失败")
    result.bytes_freed = size
    result.files_deleted = items


def _scan_event_logs(en):
    channels = event_log_channels()
    return ItemScan(records=len(channels), note="清空系统与应用日志，排查问题前不要勾选")


def _clean_event_logs(en, result):
    channels = event_log_channels()
    cleared = failed = 0
    for channel in channels:
        try:
            raw = subprocess.run(["wevtutil", "cl", channel], capture_output=True, timeout=120,
                                 creationflags=_subprocess_flags())
        except (OSError, subprocess.TimeoutExpired):
            failed += 1
            continue
        if raw.returncode == 0:
            cleared += 1
        else:
            failed += 1
    result.records_cleared = cleared
    result.note = f"已清空 {cleared} 个日志" + (f"，{failed} 个系统保留日志跳过" if failed else "")


def _scan_dns(en):
    return ItemScan(note="隐私项：清除 DNS 解析缓存，不释放磁盘空间")


def _clean_dns(en, result):
    raw = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=60,
                         creationflags=_subprocess_flags())
    if raw.returncode != 0:
        raise OSError("刷新 DNS 缓存失败（需要管理员权限）")
    result.note = "已刷新 DNS 解析缓存"


def build_items() -> list[ItemDef]:
    return [
        ItemDef("user_temp", "用户临时文件", "user", True,
                "临时目录里的文件；默认只动 24 小时前的，避免影响正在运行的程序",
                _scan_user_temp, _clean_user_temp),
        ItemDef("browser_cache", "浏览器缓存", "user", True,
                "Edge、Chrome、Firefox 的网页缓存；不碰历史、Cookie 和密码",
                _scan_browser_cache, _clean_browser_cache),
        ItemDef("thumbnails", "缩略图与图标缓存", "user", True,
                "清空后文件夹缩略图会自动重建",
                _scan_thumbnails, _clean_thumbnails),
        ItemDef("recent_docs", "最近使用记录", "user", True,
                "“最近打开”列表、跳转列表和运行历史；不影响文件本身",
                _scan_recent, _clean_recent),
        ItemDef("crash_dumps", "程序崩溃转储", "user", True,
                "应用崩溃时留下的调试转储文件",
                lambda en: _scan_contents(en, "crash_dumps",
                                          lambda: [_local_appdata() / "CrashDumps"]),
                lambda en, result: _clean_contents(en, "crash_dumps",
                                                   lambda: [_local_appdata() / "CrashDumps"], result)),
        ItemDef("wer_user", "Windows 错误报告（当前用户）", "user", True,
                "错误报告队列和存档",
                lambda en: _scan_contents(en, "wer_user", _wer_user_dirs),
                lambda en, result: _clean_contents(en, "wer_user", _wer_user_dirs, result)),
        ItemDef("recycle_bin", "回收站（C 盘）", "user", False,
                "清空后无法恢复，默认不勾选",
                _scan_recycle, _clean_recycle),
        ItemDef("windows_temp", "系统临时文件", "admin", False,
                "C:\\Windows\\Temp，需要管理员权限",
                lambda en: _scan_contents(en, "windows_temp",
                                          lambda: [_windows_root() / "Temp"]),
                lambda en, result: _clean_contents(en, "windows_temp",
                                                   lambda: [_windows_root() / "Temp"], result)),
        ItemDef("update_cache", "Windows 更新缓存", "admin", False,
                "已装更新的安装包缓存；删除后下次修复安装需要重新下载",
                lambda en: _scan_contents(en, "update_cache",
                                          lambda: [_windows_root() / "SoftwareDistribution" / "Download"]),
                lambda en, result: _clean_contents(
                    en, "update_cache",
                    lambda: [_windows_root() / "SoftwareDistribution" / "Download"], result)),
        ItemDef("prefetch", "Prefetch 预读文件", "admin", False,
                "清空后程序首次启动稍慢，系统会自动重建",
                lambda en: _scan_contents(en, "prefetch",
                                          lambda: [_windows_root() / "Prefetch"]),
                lambda en, result: _clean_contents(en, "prefetch",
                                                   lambda: [_windows_root() / "Prefetch"], result)),
        ItemDef("wer_admin", "Windows 错误报告（全部用户）", "admin", False,
                "ProgramData 下的错误报告队列和存档",
                lambda en: _scan_contents(en, "wer_admin", _wer_admin_dirs),
                lambda en, result: _clean_contents(en, "wer_admin", _wer_admin_dirs, result)),
        ItemDef("event_logs", "Windows 事件日志", "admin", False,
                "清空系统日志，排查问题前不要勾选",
                _scan_event_logs, _clean_event_logs),
        ItemDef("dns_cache", "DNS 解析缓存", "admin", False,
                "隐私项：清除本机 DNS 缓存记录",
                _scan_dns, _clean_dns),
    ]


class CleanEngine:
    """清理引擎；测试可注入自定义根目录和注册表键，避免碰真实系统。"""

    def __init__(self, root_overrides=None, registry_overrides=None, min_age_hours: float = 24):
        self.root_overrides = dict(root_overrides or {})
        self.registry_overrides = dict(registry_overrides or {})
        self.min_age_hours = min_age_hours
        self._defs = build_items()
        self._by_id = {d.id: d for d in self._defs}

    @property
    def min_age_seconds(self) -> float:
        return self.min_age_hours * 3600

    def defs(self) -> list[ItemDef]:
        return list(self._defs)

    def name(self, item_id: str) -> str:
        return self._by_id[item_id].name

    def roots(self, item_id: str, defaults) -> list[Path]:
        """优先用测试注入的根目录，否则用默认路径。"""
        if item_id in self.root_overrides:
            value = self.root_overrides[item_id]
            if isinstance(value, (str, Path)):
                return [Path(value)]
            return [Path(p) for p in value]
        return [Path(p) for p in defaults()]

    def scan(self, item_id: str) -> ItemScan:
        try:
            return self._by_id[item_id].scan(self)
        except OSError as exc:
            hint = str(exc) or "系统拒绝访问"
            return ItemScan(note=f"无法扫描：{hint}（管理员项需要授权后才能看大小）")
        except Exception as exc:  # 单项扫描失败不影响其它项
            return ItemScan(note=f"扫描出错：{type(exc).__name__}: {exc}")

    def clean(self, item_id: str) -> ItemResult:
        result = ItemResult(item=item_id)
        try:
            self._by_id[item_id].clean(self, result)
        except OSError as exc:
            result.ok = False
            result.error = str(exc) or "系统拒绝访问（可能需要管理员权限）"
        except Exception as exc:
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
        return result


# ---------------------------------------------------------------------------
# 管理员任务：主进程写任务文件 → UAC 启动独立进程 → 结果写回文件
# ---------------------------------------------------------------------------

def run_admin_task(task: dict) -> dict:
    """在当前进程执行管理员清理任务，返回结果字典。

    任务文件里只允许出现 ADMIN_ITEM_IDS 里的项目 id。根目录覆盖仅在
    非提权进程生效（供测试和无提权试运行使用）；真正提权执行时忽略，
    保证管理员权限只用于本模块预定义的固定路径。
    """
    ids = [i for i in task.get("items", []) if i in ADMIN_ITEM_IDS]
    overrides = task.get("roots") if not is_admin() else None
    engine = CleanEngine(root_overrides=overrides)
    return {"ok": True, "results": [dataclasses.asdict(engine.clean(i)) for i in ids]}


def run_admin_task_entry(argv: list[str]) -> int:
    """`文件工具箱 --admin-task 任务文件` 的入口；无论成败都写出结果文件。"""
    try:
        flag = argv.index(ADMIN_TASK_FLAG)
        task_path = Path(argv[flag + 1])
    except (ValueError, IndexError):
        return 1
    result_path = Path(str(task_path) + ".result.json")
    outcome: dict
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
        try:
            outcome = run_admin_task(task)
        except Exception as exc:
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        outcome = {"ok": False, "error": f"无法读取任务文件：{exc}"}
    try:
        result_path.write_text(json.dumps(outcome, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return 0


def _elevated_command(task_path: Path) -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        return str(sys.executable), f'{ADMIN_TASK_FLAG} "{task_path}"'
    script = Path(__file__).resolve()
    interp = Path(sys.executable)
    windowed = interp.with_name("pythonw.exe")
    if windowed.is_file():
        interp = windowed
    return str(interp), f'"{script}" {ADMIN_TASK_FLAG} "{task_path}"'


def start_admin_task(items: list[str]) -> tuple[Path, Path]:
    """发起管理员清理任务（弹 UAC），返回 (任务文件, 结果文件)。"""
    if os.name != "nt":
        raise OSError("管理员任务仅适用于 Windows。")
    task_path = Path(tempfile.gettempdir()) / f"toolbox_clean_task_{uuid.uuid4().hex}.json"
    result_path = Path(str(task_path) + ".result.json")
    task_path.write_text(json.dumps({"items": list(items)}, ensure_ascii=False),
                         encoding="utf-8")
    launcher, args = _elevated_command(task_path)
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", launcher, args, None, 0)  # SW_HIDE
    if rc <= 32:
        task_path.unlink(missing_ok=True)
        raise OSError("未能启动管理员任务（可能取消了授权）。")
    return task_path, result_path


def wait_for_result(result_path: Path, timeout_seconds: float = 1800,
                    progress=None, poll: float = 0.5) -> dict | None:
    """轮询管理员任务的结果文件；超时返回 None。"""
    start = time.time()
    deadline = start + timeout_seconds
    last_note = 0.0
    while time.time() < deadline:
        if result_path.exists():
            for _ in range(5):  # 结果文件可能正写到一半
                try:
                    return json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    time.sleep(0.2)
            return None
        if progress is not None and time.time() - last_note > 10:
            last_note = time.time()
            progress(f"管理员任务执行中… 已等待 {int(time.time() - start)} 秒")
        time.sleep(poll)
    return None


def merge_admin_outcome(outcome: dict | None, item_ids: list[str]) -> dict[str, ItemResult]:
    """把管理员任务结果合并成 {项目 id: ItemResult}；缺失或失败都有交代。"""
    if outcome is None:
        message = "等待管理员任务超时，或取消了授权"
        return {iid: ItemResult(item=iid, ok=False, error=message) for iid in item_ids}
    if not outcome.get("ok"):
        message = outcome.get("error", "管理员任务失败")
        return {iid: ItemResult(item=iid, ok=False, error=message) for iid in item_ids}
    fields = {f.name for f in dataclasses.fields(ItemResult)}
    got: dict[str, ItemResult] = {}
    for row in outcome.get("results", []):
        try:
            result = ItemResult(**{k: v for k, v in row.items() if k in fields})
        except Exception:
            continue
        got[result.item] = result
    return {iid: got.get(iid, ItemResult(item=iid, ok=False,
                                         error="管理员任务没有返回这一项的结果"))
            for iid in item_ids}


# ---------------------------------------------------------------------------
# 图形界面页签（tkinter 仅在用到时导入，保证 Linux 上可 import）
# ---------------------------------------------------------------------------

class CleanerPanel:
    """C 盘清理页签；组合一个 ttk.Frame，供 file_toolbox 挂到 Notebook 上。"""

    POLL_MS = 100

    def __init__(self, master, engine: CleanEngine | None = None, status_callback=None):
        import tkinter as tk
        from tkinter import ttk

        self.engine = engine or CleanEngine()
        self._status_cb = status_callback
        self._thread: threading.Thread | None = None
        self._closing = False
        self._scans: dict[str, ItemScan] = {}
        self._status_text = ""
        self._final_status = ""
        self._pending_info = ""

        self.frame = ttk.Frame(master, padding=(10, 8))

        header = ttk.Frame(self.frame)
        header.pack(fill="x")
        ttk.Label(header, text="C 盘清理", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.scan_btn = ttk.Button(header, text="① 扫描", command=self._start_scan)
        self.scan_btn.pack(side="right")
        self.clean_btn = ttk.Button(header, text="② 清理所勾选项", state="disabled",
                                    command=self._start_clean)
        self.clean_btn.pack(side="right", padx=(0, 8))

        admin_note = "" if is_admin() or os.name != "nt" else "带“管理员”的项清理时会弹出 UAC 授权窗口；"
        ttk.Label(self.frame, foreground="#666", wraplength=1050, justify="left", text=(
            "先扫描看大小，勾选后再清理；被占用的文件会自动跳过，不会影响正在运行的程序。"
            + admin_note + "只清理位置固定、可再生成的系统目录，绝不碰你的文档、密码和浏览器登录状态。")
        ).pack(fill="x", pady=(2, 6))

        self.items_frame = ttk.Frame(self.frame)
        self.items_frame.pack(fill="both", expand=True)
        self.items_frame.columnconfigure(1, weight=1)
        self.check_vars: dict[str, tk.BooleanVar] = {}
        self.size_labels: dict[str, ttk.Label] = {}
        self._defs = self.engine.defs()
        for row, item in enumerate(self._defs):
            name = item.name + ("（管理员）" if item.scope == "admin" else "")
            var = tk.BooleanVar(self.frame, value=item.default_on)
            self.check_vars[item.id] = var
            ttk.Checkbutton(self.items_frame, text=name, variable=var).grid(
                row=row, column=0, sticky="w", padx=(0, 8))
            ttk.Label(self.items_frame, text=item.description, foreground="#888").grid(
                row=row, column=1, sticky="w")
            size_label = ttk.Label(self.items_frame, text="未扫描", width=32, anchor="e")
            size_label.grid(row=row, column=2, sticky="e")
            self.size_labels[item.id] = size_label

        bottom = ttk.Frame(self.frame)
        bottom.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.status_var = tk.StringVar(self.frame, value="就绪。点“① 扫描”先看看每类垃圾有多大。")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", fill="x", expand=True)

    def busy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def shutdown(self) -> None:
        self._closing = True

    def _set_status(self, text: str) -> None:
        self._status_text = text

    def _app_status(self, text: str) -> None:
        if self._status_cb is not None:
            self._status_cb(text)

    def _size_text(self, scan: ItemScan | None) -> str:
        if scan is None:
            return "未扫描"
        parts = []
        if scan.bytes:
            parts.append(fmt_size(scan.bytes))
        if scan.files:
            parts.append(f"{scan.files} 个文件")
        if scan.records:
            parts.append(f"{scan.records} 条记录")
        text = "（" + "，".join(parts) + "）" if parts else "（0 B）"
        if scan.note:
            text = (text + " " + scan.note).strip()
        return text

    def _refresh_labels(self) -> None:
        for item in self._defs:
            self.size_labels[item.id].configure(text=self._size_text(self._scans.get(item.id)))

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_btn.configure(state=state)
        clean_state = "disabled" if busy or not self._scans else "normal"
        self.clean_btn.configure(state=clean_state)
        if busy:
            self.progress.pack(side="right", padx=(0, 10))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _start_job(self, job) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._work_main, args=(job,), daemon=True)
        self._thread.start()
        self.frame.after(self.POLL_MS, self._poll)

    def _work_main(self, job) -> None:
        try:
            job()
        except Exception as exc:  # 任何意外都反映到状态栏，不让线程无声死掉
            self._final_status = f"出错：{type(exc).__name__}: {exc}"

    def _poll(self) -> None:
        from tkinter import TclError
        try:
            if self._status_text:
                self.status_var.set(self._status_text)
                self._app_status(f"清理页：{self._status_text}")
                self._status_text = ""
            if self._thread and self._thread.is_alive():
                self.frame.after(self.POLL_MS, self._poll)
                return
            self._thread = None
            self._refresh_labels()
            self._set_busy(False)
            if self._final_status:
                self.status_var.set(self._final_status)
                self._app_status(f"清理页：{self._final_status}")
                self._final_status = ""
            info, self._pending_info = self._pending_info, ""
            if info and not self._closing:
                from tkinter import messagebox
                messagebox.showinfo("清理结果", info, parent=self.frame)
        except TclError:
            self._thread = None  # 窗口已销毁，安静退出

    # ---- 扫描 ----

    def _start_scan(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._scans.clear()
        self._set_busy(True)
        self.status_var.set("正在扫描…")

        def job():
            total = 0
            for item in self._defs:
                if self._closing:
                    return
                self._set_status(f"正在扫描：{item.name}")
                scan = self.engine.scan(item.id)
                self._scans[item.id] = scan
                total += scan.bytes
            self._final_status = f"扫描完成：全部项目合计约 {fmt_size(total)}，勾选后点“② 清理所勾选项”。"

        self._start_job(job)

    # ---- 清理 ----

    def _confirm_clean(self, lines: list[str], total_bytes: int,
                       has_admin: bool, has_recycle: bool) -> bool:
        text = "将永久删除以下内容：\n\n" + "\n".join(lines)
        text += f"\n\n合计约 {fmt_size(total_bytes)}。"
        if has_recycle:
            text += "\n\n注意：回收站清空后无法恢复。"
        if has_admin and not is_admin():
            text += "\n\n包含管理员级项目，接下来会弹出 UAC 授权窗口，请选择“是”。"
        from tkinter import messagebox
        return messagebox.askyesno("确认清理", text, icon="warning", parent=self.frame)

    def _start_clean(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        checked = [d for d in self._defs if self.check_vars[d.id].get()]
        if not checked:
            from tkinter import messagebox
            messagebox.showinfo("没有勾选", "请先勾选要清理的项目。", parent=self.frame)
            return
        lines = [f"· {d.name}：{self._size_text(self._scans.get(d.id))}" for d in checked]
        total = sum(self._scans[d.id].bytes for d in checked if d.id in self._scans)
        has_admin = any(d.scope == "admin" for d in checked)
        has_recycle = any(d.id == "recycle_bin" for d in checked)
        if not self._confirm_clean(lines, total, has_admin, has_recycle):
            return
        self._set_busy(True)
        self.status_var.set("正在清理…")

        def job():
            results = self._clean_all(checked)
            freed = sum(r.bytes_freed for r in results.values())
            ok_count = sum(1 for r in results.values() if r.ok)
            self._pending_info = self._summarize(checked, results)
            self._final_status = (f"清理完成：{ok_count}/{len(checked)} 项成功，"
                                  f"共释放约 {fmt_size(freed)}。详情见弹窗。")
            if not self._closing:
                self._set_status("重新扫描最新大小…")
                for item in self._defs:
                    if self._closing:
                        return
                    self._scans[item.id] = self.engine.scan(item.id)

        self._start_job(job)

    def _clean_all(self, checked) -> dict[str, ItemResult]:
        results: dict[str, ItemResult] = {}
        admin_ids = [d.id for d in checked if d.scope == "admin"]
        user_ids = [d.id for d in checked if d.scope != "admin"]

        def run_list(ids):
            for iid in ids:
                if self._closing:
                    return
                self._set_status(f"正在清理：{self.engine.name(iid)}")
                results[iid] = self.engine.clean(iid)

        if is_admin() or os.name != "nt":
            run_list(user_ids + admin_ids)
        else:
            run_list(user_ids)
            if admin_ids and not self._closing:
                self._set_status("正在请求管理员授权…（在 UAC 弹窗中选择“是”）")
                try:
                    task_path, result_path = start_admin_task(admin_ids)
                except OSError as exc:
                    for iid in admin_ids:
                        results[iid] = ItemResult(item=iid, ok=False, error=str(exc))
                else:
                    self._set_status("管理员任务已启动，正在执行…")
                    outcome = wait_for_result(result_path, progress=self._set_status)
                    task_path.unlink(missing_ok=True)
                    result_path.unlink(missing_ok=True)
                    results.update(merge_admin_outcome(outcome, admin_ids))
        return results

    def _summarize(self, checked, results: dict[str, ItemResult]) -> str:
        lines = []
        for d in checked:
            r = results.get(d.id)
            if r is None:
                lines.append(f"· {d.name}：已跳过（任务被中断）")
            elif not r.ok:
                lines.append(f"· {d.name}：失败（{r.error}）")
            else:
                detail = []
                if r.bytes_freed:
                    detail.append(f"释放 {fmt_size(r.bytes_freed)}")
                if r.files_deleted:
                    detail.append(f"删除 {r.files_deleted} 个文件")
                if r.records_cleared:
                    detail.append(f"清除 {r.records_cleared} 条记录")
                if r.skipped:
                    detail.append(f"跳过 {r.skipped} 个占用文件")
                lines.append(f"· {d.name}：" + ("；".join(detail) if detail else (r.note or "无需处理")))
        freed = sum(r.bytes_freed for r in results.values())
        return "清理明细：\n" + "\n".join(lines) + f"\n\n共释放约 {fmt_size(freed)}。"
