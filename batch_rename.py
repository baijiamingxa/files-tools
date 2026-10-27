#!/usr/bin/env python3
"""批量文件重命名：只预览、检查冲突，再分两阶段改名；不覆盖文件。"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class RenameOptions:
    mode: str = "keep"  # keep / fixed / date
    base_name: str = ""
    date_format: str = "%Y%m%d_%H%M%S"
    find: str = ""
    replacement: str = ""
    prefix: str = ""
    suffix: str = ""
    numbering: bool = False
    start: int = 1
    step: int = 1
    width: int = 3
    number_position: str = "suffix"
    separator: str = "_"
    extension: str = ""  # 空白保留最后一个扩展名


@dataclass(frozen=True)
class RenameRow:
    src: Path
    dst: Path
    signature: tuple
    error: str = ""

    @property
    def changed(self):
        return self.src.name != self.dst.name


@dataclass
class RenameResult:
    rows: list
    undo_rows: list
    record_path: Path | None


class RenameError(Exception):
    pass


def path_key(path):
    # 同时在 Linux CI 上按 Windows 的文件名冲突规则检查。
    return os.path.abspath(os.fspath(path)).casefold()


def file_signature(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RenameError("只处理普通文件，跳过链接和目录")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def invalid_name(name):
    if not name or name in (".", ".."):
        return "文件名不能为空或为点目录"
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        return "文件名含 Windows 不允许的字符"
    if name.endswith((" ", ".")):
        return "文件名不能以空格或点结尾"
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{base}{i}" for base in ("COM", "LPT") for i in range(1, 10)}
    if name.split(".")[0].rstrip(" .").upper() in reserved:
        return "文件名是 Windows 保留名称"
    if len(name.encode("utf-16-le")) // 2 > 255 or (os.name != "nt" and len(os.fsencode(name)) > 255):
        return "文件名过长"
    return ""


def collect_files(inputs, recursive=False, extensions="all", stop=None):
    wanted = {"." + part.strip().lower().lstrip(".") for part in extensions.split(",") if part.strip()}
    all_types = not extensions.strip() or extensions.strip().lower() == "all"
    found = {}
    pending = [Path(os.path.abspath(os.fspath(p))) for p in inputs]
    while pending:
        if stop is not None and stop.is_set():
            raise RenameError("已取消扫描")
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            continue
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False) or (recursive and entry.is_dir(follow_symlinks=False)):
                        pending.append(Path(entry.path))
        elif stat.S_ISREG(info.st_mode) and (all_types or path.suffix.lower() in wanted):
            found.setdefault(path_key(path), path)
    return sorted(found.values(), key=lambda p: (path_key(p.parent), p.name.casefold(), p.name))


def _target_name(path, options, index, modified):
    if options.mode == "fixed":
        base = options.base_name
    elif options.mode == "date":
        base = datetime.fromtimestamp(modified).strftime(options.date_format)
    elif options.mode == "keep":
        base = path.stem
    else:
        raise RenameError("未知的命名方式")
    if options.find:
        base = base.replace(options.find, options.replacement)
    if options.numbering:
        number = str(options.start + index * options.step).zfill(options.width)
        if options.number_position == "prefix":
            base = number + (options.separator if base else "") + base
        else:
            base += (options.separator if base else "") + number
    extension = options.extension
    extension = "." + extension.lstrip(".") if extension else path.suffix
    return options.prefix + base + options.suffix + extension


def check_rows(rows, stop=None):
    errors, sources, targets, siblings = {}, {}, {}, {}
    for index, row in enumerate(rows):
        if stop is not None and stop.is_set():
            raise RenameError("已取消检查，文件名未修改")
        if row.error:
            errors[index] = row.error
            continue
        reason = invalid_name(row.dst.name)
        if row.src.parent != row.dst.parent:
            reason = "只能在原文件夹内改名"
        try:
            if file_signature(row.src) != row.signature:
                reason = "文件已被修改或替换，请重新预览"
        except (OSError, RenameError) as exc:
            reason = f"无法读取源文件：{exc}"
        if reason:
            errors[index] = reason
        sources[path_key(row.src)] = (index, row)
        targets.setdefault(path_key(row.dst), []).append(index)
    for indexes in targets.values():
        if len(indexes) > 1:
            for index in indexes:
                errors[index] = "多个文件将使用同一个名字（不区分大小写）"
    for index, row in enumerate(rows):
        if index in errors:
            continue
        try:
            parent = path_key(row.src.parent)
            if parent not in siblings:
                with os.scandir(row.src.parent) as entries:
                    siblings[parent] = {entry.name.casefold(): Path(entry.path) for entry in entries}
            occupied = siblings[parent].get(row.dst.name.casefold())
            if occupied is not None:
                moving = sources.get(path_key(occupied))
                if moving is None or (moving[0] != index and (not moving[1].changed or moving[0] in errors)):
                    errors[index] = "目标名称已被其他文件或文件夹占用"
        except OSError as exc:
            errors[index] = str(exc)
    return errors


def build_plan(paths, options, stop=None):
    if options.width < 1 or options.width > 12 or options.start < 0 or options.step < 1:
        raise RenameError("序号起始值须非负，步长须为正，位数为 1～12")
    if options.mode == "fixed" and not options.base_name and not options.numbering:
        raise RenameError("请填写统一名称，或启用序号")
    rows = []
    for index, path in enumerate(paths):
        if stop is not None and stop.is_set():
            raise RenameError("已取消预览，文件名未修改")
        path = Path(os.path.abspath(os.fspath(path)))
        try:
            signature = file_signature(path)
            name = _target_name(path, options, index, path.stat().st_mtime)
            # 不使用 with_name，先把非法路径字符作为一行冲突显示出来。
            error = invalid_name(name)
            rows.append(RenameRow(path, path.parent / name, signature, error))
        except (OSError, ValueError, RenameError) as exc:
            rows.append(RenameRow(path, path, (), str(exc)))
    errors = check_rows(rows, stop)
    return [replace(row, error=errors.get(index, "")) for index, row in enumerate(rows)]


def _move_no_replace(source, target):
    if os.name == "nt":
        os.rename(source, target)  # Windows rename 遇到已有目标会报错。
    else:
        # POSIX rename 会覆盖目标，使用独占创建硬链接来避免覆盖竞争。
        os.link(source, target, follow_symlinks=False)
        try:
            os.unlink(source)
        except OSError:
            os.unlink(target)
            raise


def _write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".rename-record-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def execute_plan(rows, record_dir, stop=None, progress=None):
    errors = check_rows(rows, stop)
    if errors:
        raise RenameError("预览已失效或存在冲突，请重新预览：\n" + "\n".join(f"{rows[i].src.name}：{text}" for i, text in list(errors.items())[:10]))
    changing = [row for row in rows if row.changed]
    if not changing:
        return RenameResult(list(rows), [], None)
    token = uuid.uuid4().hex
    temps = [row.src.with_name(f".toolbox-rename-{token}-{i}.tmp") for i, row in enumerate(changing)]
    locations = [row.src for row in changing]
    record_path = Path(record_dir) / f"重命名_{datetime.now():%Y%m%d_%H%M%S}_{token[:8]}.json"
    record = {"status": "prepared", "files": [dict(source=str(row.src), target=str(row.dst), temporary=str(temp), signature=list(row.signature)) for row, temp in zip(changing, temps)]}
    _write_record(record_path, record)  # 写入操作记录成功后才开始改名。
    try:
        for index, row in enumerate(changing):
            if stop is not None and stop.is_set():
                raise RenameError("已取消，正在恢复原文件名")
            if file_signature(row.src) != row.signature:
                raise RenameError(f"源文件已变化：{row.src}")
            _move_no_replace(row.src, temps[index])
            locations[index] = temps[index]
        for index, row in enumerate(changing):
            if stop is not None and stop.is_set():
                raise RenameError("已取消，正在恢复原文件名")
            _move_no_replace(temps[index], row.dst)
            locations[index] = row.dst
            if progress:
                progress(index + 1, len(changing))
        undo = [RenameRow(row.dst, row.src, file_signature(row.dst)) for row in changing]
        record["status"] = "completed"
        _write_record(record_path, record)
        return RenameResult(list(rows), undo, record_path)
    except Exception as exc:
        recovery_errors = []
        # 先把已经落到最终名称的文件重新移到临时名，才能安全回退交换/循环改名。
        for index, row in enumerate(changing):
            if str(locations[index]) in (str(row.src), str(temps[index])):
                continue
            try:
                _move_no_replace(locations[index], temps[index])
                locations[index] = temps[index]
            except OSError as error:
                recovery_errors.append(str(error))
        for index, row in enumerate(changing):
            if str(locations[index]) == str(row.src):
                continue
            try:
                _move_no_replace(locations[index], row.src)
                locations[index] = row.src
            except OSError as error:
                recovery_errors.append(f"{locations[index]} → {row.src}：{error}")
        record.update(status="recovery_needed" if recovery_errors else "rolled_back", error=str(exc), locations=[str(path) for path in locations], recovery_errors=recovery_errors)
        try:
            _write_record(record_path, record)
        except OSError:
            pass
        detail = "\n恢复未全部完成：\n" + "\n".join(recovery_errors) if recovery_errors else "\n已恢复本次操作的原文件名。"
        raise RenameError(f"{exc}{detail}\n操作记录：{record_path}") from exc
