#!/usr/bin/env python3
"""目录空间分析、SHA-256 查重与带快照复查的 Windows 回收操作。"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CHUNK = 1024 * 1024
SKIP_DIRS = frozenset({"$recycle.bin", "system volume information"})


class StorageError(Exception):
    pass


class ScanCancelled(StorageError):
    pass


def key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def under(path, parent):
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


def cancelled(stop):
    if stop is not None and stop.is_set():
        raise ScanCancelled("已停止；没有执行文件删除。")


def _plain(info):
    return not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    mtime_ns: int
    identity: tuple
    links: int = 1


@dataclass
class FolderInfo:
    path: Path
    identity: tuple
    size: int = 0
    files: int = 0
    folders: int = 0


@dataclass
class ScanResult:
    root: Path
    files: list = field(default_factory=list)
    folders: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    skipped_links: int = 0
    skipped_system: int = 0

    @property
    def total_size(self):
        return self.folders[key(self.root)].size


@dataclass(frozen=True)
class DuplicateGroup:
    digest: str
    files: tuple

    @property
    def keeper(self):
        return min(self.files, key=lambda file: (-file.mtime_ns, key(file.path)))

    @property
    def potential_size(self):
        keeper = self.keeper
        return sum(file.size for file in self.files if file is not keeper and file.links == 1)


@dataclass
class DuplicateResult:
    scan: ScanResult
    groups: list
    errors: list
    hardlink_aliases: int = 0
    hashed_files: int = 0


@dataclass
class DeleteResult:
    removed: list
    remaining: list
    message: str = ""


def scan_tree(root, stop=None, progress=None):
    root = Path(root).expanduser().resolve(strict=True)
    info = root.lstat()
    if not _plain(info) or not stat.S_ISDIR(info.st_mode):
        raise StorageError("请选择普通文件夹或磁盘根目录。")
    result = ScanResult(root)
    result.folders[key(root)] = FolderInfo(root, signature(info))
    stack, visited, last_update = [root], [], 0.0
    direct_bytes = 0
    while stack:
        cancelled(stop)
        folder = stack.pop()
        visited.append(folder)
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    cancelled(stop)
                    path = Path(entry.path)
                    try:
                        item = entry.stat(follow_symlinks=False)
                        # Windows 的 DirEntry 缓存可能把设备号、文件号及链接数都返回为 0。
                        # 补取完整属性，供硬链接去重和操作前的身份复查使用。
                        if not item.st_ino or not item.st_nlink:
                            item = path.lstat()
                        if not _plain(item):
                            result.skipped_links += 1
                            continue
                        if stat.S_ISDIR(item.st_mode):
                            if entry.name.casefold() in SKIP_DIRS:
                                result.skipped_system += 1
                                continue
                            result.folders[key(path)] = FolderInfo(path, signature(item))
                            stack.append(path)
                        elif stat.S_ISREG(item.st_mode):
                            result.files.append(FileInfo(path, item.st_size, item.st_mtime_ns, signature(item), item.st_nlink))
                            result.folders[key(folder)].size += item.st_size
                            result.folders[key(folder)].files += 1
                            direct_bytes += item.st_size
                    except OSError as exc:
                        result.errors.append((path, str(exc)))
        except OSError as exc:
            result.errors.append((folder, str(exc)))
        if progress and time.monotonic() - last_update > 0.12:
            progress("scan", len(result.files), direct_bytes, str(folder))
            last_update = time.monotonic()
    for folder in reversed(visited):
        if folder != root:
            child, parent = result.folders[key(folder)], result.folders[key(folder.parent)]
            parent.size += child.size
            parent.files += child.files
            parent.folders += child.folders + 1
    cancelled(stop)
    return result


def verify_file(file):
    try:
        current = file.path.lstat()
    except OSError as exc:
        raise StorageError(f"文件无法访问，请重新扫描：{file.path}\n{exc}") from exc
    if not _plain(current) or not stat.S_ISREG(current.st_mode) or signature(current) != file.identity:
        raise StorageError(f"文件已被修改、替换或变成链接，请重新扫描：{file.path}")


def hash_file(file, stop=None, progress=None):
    verify_file(file)
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(file.path, flags)
    with os.fdopen(descriptor, "rb") as source:
        if signature(os.fstat(source.fileno())) != file.identity:
            raise StorageError(f"读取前文件已变化：{file.path}")
        while True:
            cancelled(stop)
            block = source.read(CHUNK)
            if not block:
                break
            digest.update(block)
            if progress:
                progress(len(block))
        if signature(os.fstat(source.fileno())) != file.identity:
            raise StorageError(f"读取过程中文件已变化：{file.path}")
    verify_file(file)
    return digest.hexdigest()


def find_duplicates(scan, stop=None, progress=None, include_empty=False, min_size=0):
    by_size, identities, aliases = defaultdict(list), set(), 0
    for file in scan.files:
        cancelled(stop)
        if file.size < min_size or (file.size == 0 and not include_empty):
            continue
        physical = file.identity[:2]
        if physical[1] and physical in identities:
            aliases += 1
            continue
        identities.add(physical)
        by_size[file.size].append(file)
    candidates = [files for files in by_size.values() if len(files) > 1]
    total = sum(file.size for files in candidates for file in files)
    done, last_update = 0, 0.0
    def advanced(amount):
        nonlocal done, last_update
        done += amount
        if progress and time.monotonic() - last_update > 0.12:
            progress("hash", done, total, "正在读取候选文件并计算 SHA-256")
            last_update = time.monotonic()
    groups, errors, hashed = [], [], 0
    for files in sorted(candidates, key=lambda group: -group[0].size):
        hashes = defaultdict(list)
        for file in sorted(files, key=lambda item: key(item.path)):
            cancelled(stop)
            try:
                hashes[hash_file(file, stop, advanced)].append(file)
                hashed += 1
            except (OSError, StorageError) as exc:
                if isinstance(exc, ScanCancelled):
                    raise
                errors.append((file.path, str(exc)))
        for digest, same in hashes.items():
            if len(same) > 1:
                groups.append(DuplicateGroup(digest, tuple(same)))
    groups.sort(key=lambda group: (-group.potential_size, group.digest))
    return DuplicateResult(scan, groups, errors, aliases, hashed)


def selected_entries(scan, paths):
    entries = {key(file.path): file for file in scan.files}
    entries.update({k: folder for k, folder in scan.folders.items() if k != key(scan.root)})
    chosen = {}
    for path in paths:
        path = Path(os.path.abspath(os.fspath(path)))
        if key(path) not in entries or path == path.parent or key(path) == key(scan.root):
            raise StorageError(f"只能选择扫描结果中的文件或子文件夹：{path}")
        chosen[key(path)] = entries[key(path)]
    ordered = sorted(chosen.values(), key=lambda entry: (len(entry.path.parts), key(entry.path)))
    result = []
    for entry in ordered:
        if not any(isinstance(parent, FolderInfo) and under(entry.path, parent.path) for parent in result):
            result.append(entry)
    if not result:
        raise StorageError("请先选择要处理的文件或子文件夹。")
    return result


def _verify_folder(scan, folder, stop):
    if any(under(path, folder.path) for path, _message in scan.errors):
        raise StorageError(f"该文件夹含未能扫描的内容，请先打开位置检查：{folder.path}")
    current = folder.path.lstat()
    if not _plain(current) or signature(current) != folder.identity:
        raise StorageError(f"文件夹已变化，请重新扫描：{folder.path}")
    fresh = scan_tree(folder.path, stop)
    if fresh.skipped_links or fresh.skipped_system:
        raise StorageError(f"文件夹含链接或未扫描的系统管理目录，请打开位置检查后手动处理：{folder.path}")
    old_files = {key(file.path): file.identity for file in scan.files if under(file.path, folder.path)}
    new_files = {key(file.path): file.identity for file in fresh.files}
    old_folders = {k: entry.identity for k, entry in scan.folders.items() if under(entry.path, folder.path)}
    new_folders = {k: entry.identity for k, entry in fresh.folders.items()}
    if fresh.errors or old_files != new_files or old_folders != new_folders:
        raise StorageError(f"文件夹内容已变化，请重新扫描：{folder.path}")


def prepare_delete(scan, paths, groups=None, stop=None):
    entries = selected_entries(scan, paths)
    selected = {key(entry.path) for entry in entries}
    if groups is not None:
        by_path = {key(file.path): group for group in groups for file in group.files}
        if any(isinstance(entry, FolderInfo) or key(entry.path) not in by_path for entry in entries):
            raise StorageError("查重结果只能删除已校验的重复文件。")
        touched = {id(by_path[k]): by_path[k] for k in selected}
        for group in touched.values():
            retained = [file for file in group.files if key(file.path) not in selected]
            if not retained:
                raise StorageError("每组重复文件至少保留一份，不能全部删除。")
            # 删除前重新校验保留副本及待处理副本，防止保留文件已被改写或移走。
            for file in [retained[0], *[file for file in group.files if key(file.path) in selected]]:
                cancelled(stop)
                if hash_file(file, stop) != group.digest:
                    raise StorageError(f"重复文件内容已变化，请重新查重：{file.path}")
    for entry in entries:
        cancelled(stop)
        if isinstance(entry, FileInfo):
            verify_file(entry)
        else:
            _verify_folder(scan, entry, stop)
    return [entry.path for entry in entries]


def _shell_request(paths):
    import ctypes
    from ctypes import wintypes
    class Operation(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD), ("aborted", wintypes.BOOL), ("mappings", ctypes.c_void_p), ("title", wintypes.LPCWSTR)]
    absolute = [os.path.abspath(os.fspath(path)) for path in paths]
    if not absolute:
        raise StorageError("没有要处理的文件。")
    if any("\0" in path or "*" in path or "?" in path for path in absolute):
        raise StorageError("删除路径不能包含 NUL 或通配符。")
    buffer = ctypes.create_unicode_buffer("\0".join(absolute) + "\0\0")
    operation = Operation()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
    operation.fFlags = 0x0040 | 0x4000  # ALLOWUNDO | WANTNUKEWARNING，保留系统确认和错误界面。
    return operation, buffer


def recycle_paths(paths):
    if os.name != "nt":
        raise StorageError("文件回收操作仅支持 Windows；请使用“打开所在位置”手动处理。")
    import ctypes
    operation, buffer = _shell_request(paths)
    ole = ctypes.windll.ole32
    initialized = ole.CoInitializeEx(None, 2)
    if initialized not in (0, 1):
        raise StorageError(f"无法初始化 Windows 文件操作：{initialized}")
    try:
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if operation.aborted:
            return "已取消或只完成了部分系统文件操作。"
        return f"系统文件操作返回错误 0x{code:08X}。" if code else ""
    finally:
        ole.CoUninitialize()


def delete_selected(scan, paths, groups=None, stop=None, recycler=None):
    checked = prepare_delete(scan, paths, groups, stop)
    cancelled(stop)
    try:
        message = (recycler or recycle_paths)(checked) or ""
    except Exception as exc:
        message = str(exc)
    removed = [path for path in checked if not os.path.lexists(path)]
    remaining = [path for path in checked if os.path.lexists(path)]
    return DeleteResult(removed, remaining, message)
