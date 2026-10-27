#!/usr/bin/env python3
"""批量文本替换和文件校验核心；流式处理、严格编码、预览后复查。"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from batch_rename import collect_files, file_signature
import txt_encoder_core as encoder

CHUNK = 1024 * 1024


class FileToolError(Exception):
    pass


class FileToolCancelled(FileToolError):
    pass


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise FileToolCancelled("已取消；已完成的项目保留，未完成的文件不覆盖。")


def key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _hash(name):
    try:
        return hashlib.new(name, usedforsecurity=False)
    except TypeError:
        return hashlib.new(name)


def file_hashes(path, algorithms=("md5", "sha256"), stop=None, progress=None):
    if not algorithms or any(name not in ("md5", "sha256") for name in algorithms):
        raise FileToolError("请选择 MD5 和 / 或 SHA-256。")
    before = file_signature(path)
    digests = {name: _hash(name) for name in algorithms}
    done = 0
    with open(path, "rb") as source:
        stat = os.fstat(source.fileno())
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != before:
            raise FileToolError("文件在打开时已改变，请重新扫描。")
        while True:
            check_stop(stop)
            block = source.read(CHUNK)
            if not block:
                break
            for digest in digests.values():
                digest.update(block)
            done += len(block)
            if progress:
                progress(done, before[2])
    if file_signature(path) != before:
        raise FileToolError("文件在计算过程中被修改，结果已作废。")
    return {name: digest.hexdigest() for name, digest in digests.items()}


@dataclass(frozen=True)
class ReplaceOptions:
    find: str
    replacement: str
    encoding: str = "auto"
    inplace: bool = False

    def validate(self):
        if not self.find:
            raise FileToolError("查找内容不能为空；替换内容可以留空。")
        if max(len(self.find), len(self.replacement)) > 65536:
            raise FileToolError("每条查找 / 替换内容最多 65536 个字符。")
        if self.find == self.replacement:
            raise FileToolError("查找内容和替换内容相同，无需执行。")


@dataclass
class ReplaceRow:
    path: Path
    destination: Path
    signature: tuple = ()
    digest: str = ""
    encoding: str = ""
    bom: bytes = b""
    count: int = 0
    preview: str = ""
    status: str = "未命中"
    error: str = ""
    backup: str = ""
    options: object = None


def _encoding(path, specified):
    with open(path, "rb") as source:
        head = source.read(4)
    bom, bom_codec = b"", ""
    for mark, codec, _ in encoder.BOM_ENTRIES:
        if head.startswith(mark):
            bom, bom_codec = mark, codec
            break
    detected = encoder.detect_file(path) if specified == "auto" else None
    if detected and detected.confidence < 0.8:
        raise FileToolError(f"编码检测置信度 {detected.confidence_pct}，请指定源编码后预览。")
    codec = encoder.canonical_codec(detected.encoding if detected else specified)
    if bom:
        base = "utf-8" if bom_codec == "utf-8-sig" else bom_codec
        if codec not in (base, bom_codec, base.rsplit("-", 1)[0]):
            raise FileToolError("指定编码与文件 BOM 不一致。")
        codec = base
    elif codec == "utf-8-sig":
        codec = "utf-8"
    elif codec in ("utf-16", "utf-32"):
        raise FileToolError("无 BOM 的文件请明确指定 UTF-16 LE / BE 等字节序。")
    return codec, bom


def _transform(row, options, stop=None, output=None):
    """保留匹配边界尾部，跨读取块仍按 str.replace 的非重叠语义处理。"""
    decoder = codecs.getincrementaldecoder(row.encoding)(errors="strict")
    writer = codecs.getincrementalencoder(row.encoding)(errors="strict")
    digest = hashlib.sha256()
    count, pending, preview = 0, "", ""
    if output is not None:
        output.write(row.bom)
    with open(row.path, "rb") as source:
        prefix = source.read(len(row.bom))
        if prefix != row.bom:
            raise FileToolError("文件 BOM 已改变，请重新预览。")
        digest.update(prefix)
        while True:
            check_stop(stop)
            raw = source.read(CHUNK)
            digest.update(raw)
            text = decoder.decode(raw, final=not raw)
            if "\x00" in text:
                raise FileToolError("内容含 NUL 字符，已按非文本文件跳过。")
            pending += text
            boundary = len(pending) if not raw else max(0, len(pending) - len(options.find) + 1)
            cursor = 0
            parts = []
            while True:
                index = pending.find(options.find, cursor)
                if index < 0 or index >= boundary:
                    break
                parts.extend((pending[cursor:index], options.replacement))
                count += 1
                if not preview:
                    left = pending[max(0, index - 30):index]
                    right = pending[index + len(options.find):index + len(options.find) + 30]
                    preview = f"{(left + options.find[:120] + right)!r} → {(left + options.replacement[:120] + right)!r}"
                cursor = index + len(options.find)
            consumed = max(cursor, boundary)
            parts.append(pending[cursor:consumed])
            changed = "".join(parts)
            encoded = writer.encode(changed, final=not raw)
            if output is not None:
                output.write(encoded)
            pending = pending[consumed:]
            if not raw:
                break
    return count, digest.hexdigest(), preview


def preview_replacements(paths, options, stop=None, progress=None):
    options.validate()
    rows = []
    for number, path in enumerate(paths, 1):
        check_stop(stop)
        path = Path(path)
        destination = path if options.inplace else path.with_name(path.stem + "_替换" + path.suffix)
        row = ReplaceRow(path, destination, options=options)
        try:
            row.signature = file_signature(path)
            row.encoding, row.bom = _encoding(path, options.encoding)
            row.count, row.digest, row.preview = _transform(row, options, stop)
            if file_signature(path) != row.signature:
                raise FileToolError("文件在预览时被修改，请重新预览。")
            if row.count and not options.inplace and destination.exists():
                raise FileToolError("副本目标已存在，不会覆盖；请先移动已有副本。")
            row.status = "待替换" if row.count else "未命中"
        except FileToolCancelled:
            raise
        except Exception as exc:
            row.status, row.error = "跳过 / 失败", str(exc)
        rows.append(row)
        if progress:
            progress(number, len(paths), path.name)
    return rows


def _publish_new(temp, target):
    if os.name == "nt":
        os.rename(temp, target)  # Windows rename 拒绝覆盖已有目标。
    else:
        os.link(temp, target)
        os.unlink(temp)


def execute_replacements(rows, options, stop=None, progress=None):
    options.validate()
    for number, row in enumerate(rows, 1):
        check_stop(stop)
        if row.error or not row.count or row.status != "待替换":
            continue
        temp = None
        try:
            if row.options != options:
                raise FileToolError("规则与预览不一致，请重新预览。")
            if file_signature(row.path) != row.signature:
                raise FileToolError("源文件已改变，请重新预览。")
            descriptor, temp = tempfile.mkstemp(prefix=".toolbox-replace-", dir=row.path.parent)
            with os.fdopen(descriptor, "wb") as output:
                count, digest, _ = _transform(row, options, stop, output)
                output.flush()
                os.fsync(output.fileno())
            if digest != row.digest or count != row.count or file_signature(row.path) != row.signature:
                raise FileToolError("内容与预览不一致，原文件保留，请重新预览。")
            if options.inplace:
                backup = row.path.with_name(row.path.name + ".replace-backup-" + datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:8])
                backup_complete = False
                try:
                    with open(backup, "xb") as copy, open(row.path, "rb") as source:
                        digest = hashlib.sha256()
                        while True:
                            check_stop(stop)
                            block = source.read(CHUNK)
                            if not block:
                                break
                            copy.write(block)
                            digest.update(block)
                    backup_complete = True
                finally:
                    if not backup_complete:
                        backup.unlink(missing_ok=True)
                row.backup = str(backup)
                if digest.hexdigest() != row.digest or file_signature(row.path) != row.signature:
                    backup.unlink(missing_ok=True)
                    row.backup = ""
                    raise FileToolError("备份时源文件变化，未覆盖原文件。")
                shutil.copystat(row.path, backup)
                shutil.copystat(row.path, temp)
                check_stop(stop)
                os.replace(temp, row.path)
            else:
                check_stop(stop)
                _publish_new(temp, row.destination)
            temp = None
            row.status = "已替换"
        except FileToolCancelled:
            raise
        except Exception as exc:
            row.status, row.error = "失败", str(exc)
        finally:
            if temp is not None:
                Path(temp).unlink(missing_ok=True)
        if progress:
            progress(number, len(rows), row.path.name)
    return rows


@dataclass
class HashRow:
    path: Path
    size: int = 0
    digests: dict = field(default_factory=dict)
    status: str = "已计算"
    error: str = ""


def calculate_hashes(paths, algorithms=("md5", "sha256"), stop=None, progress=None):
    rows = []
    for number, path in enumerate(paths, 1):
        check_stop(stop)
        row = HashRow(Path(path))
        try:
            row.size = row.path.stat().st_size
            row.digests = file_hashes(row.path, algorithms, stop,
                (lambda done, total: progress(number - 1, len(paths), f"{row.path.name} · {done:,} / {total:,} 字节")) if progress else None)
        except FileToolCancelled:
            raise
        except Exception as exc:
            row.status, row.error = "失败", str(exc)
        rows.append(row)
        if progress:
            progress(number, len(paths), row.path.name)
    return rows


def export_hashes(rows, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("path", "size", "md5", "sha256", "error"))
        for row in rows:
            writer.writerow((str(row.path.absolute()), row.size, row.digests.get("md5", ""), row.digests.get("sha256", ""), row.error))


def read_checksum_manifest(path):
    """CSV 或常见 hash [*]filename 清单；只解析，不读取清单指向的文件。"""
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise FileToolError("校验清单超过 32 MB，请分批处理。")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        text = data.decode("gb18030")
    records = []
    if text.lstrip().startswith(("path,", '"path",')):
        for row in csv.DictReader(io.StringIO(text)):
            name = row.get("path", "")
            if not name:
                raise FileToolError("CSV 中有缺失路径的记录。")
            for algorithm, size in (("md5", 32), ("sha256", 64)):
                value = row.get(algorithm, "").strip().lower()
                if value:
                    if not re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value):
                        raise FileToolError("CSV 中有无效的校验值。")
                    records.append((name, algorithm, value))
    else:
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{64})[ \t]+\*?(.+)", line)
            if not match:
                raise FileToolError(f"第 {number} 行不是 MD5 / SHA-256 校验记录。")
            value, name = match.groups()
            records.append((name, "md5" if len(value) == 32 else "sha256", value.lower()))
    if not records:
        raise FileToolError("清单中没有有效的校验记录。")
    seen = {}
    for name, algorithm, value in records:
        identity = (name, algorithm)
        if identity in seen and seen[identity] != value:
            raise FileToolError("同一文件存在相互矛盾的校验记录。")
        seen[identity] = value
    return [(name, algorithm, value) for (name, algorithm), value in seen.items()]


def compare_hashes(rows, records, base):
    computed = {key(row.path): row for row in rows}
    results = []
    for name, algorithm, expected in records:
        # 只和已经主动选取并计算的文件比较；清单不能触发额外文件读取。
        target = Path(name)
        if not target.is_absolute():
            target = Path(base) / name.replace("\\", "/")
        row = computed.get(key(target))
        if row is None:
            state = "未选择此文件"
        elif row.error:
            state = "计算失败"
        elif algorithm not in row.digests:
            state = "未计算此算法"
        else:
            state = "一致" if row.digests[algorithm] == expected else "不一致"
        results.append((name, algorithm, state, expected, row.digests.get(algorithm, "") if row else ""))
    return results
