#!/usr/bin/env python3
"""PDF 合并、按页拆分、提取及旋转；引擎只在受保护子进程中加载。"""
from __future__ import annotations

import hashlib
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from batch_rename import file_signature
from document_conversion import run_document_worker
from file_tools import FileToolError, FileToolCancelled, check_stop, key, _publish_new


def parse_pages(text, total):
    if total < 1:
        raise FileToolError("PDF 没有可用页面。")
    text = text.strip().replace("，", ",").replace("–", "-")
    if text in ("", "全部", "all"):
        return list(range(total))
    if len(text) > 10000:
        raise FileToolError("页码范围过长，请缩小范围。")
    pages, seen = [], set()
    for part in text.split(","):
        match = re.fullmatch(r"\s*([0-9]+)\s*(?:-\s*([0-9]+)\s*)?", part)
        if not match:
            raise FileToolError("页码格式有误，请填写如 1-3,5,8-10；留空表示全部。")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= total:
            raise FileToolError(f"页码超出范围或区间倒置：{part.strip()}，文档共 {total} 页。")
        for page in range(first - 1, last):
            if page in seen:
                raise FileToolError(f"页码 {page + 1} 重复，请检查范围。")
            seen.add(page)
            pages.append(page)
    return pages


@dataclass(frozen=True)
class PdfOptions:
    mode: str = "merge"
    output_dir: str = ""
    filename: str = "合并结果.pdf"
    pages: str = ""
    split_every: int = 1
    rotation: int = 90
    password: str = field(default="", repr=False)
    timeout: int = 1200

    def validate(self):
        if self.mode not in ("merge", "split", "extract", "rotate"):
            raise FileToolError("请选择 PDF 操作。")
        if not 1 <= self.split_every <= 1000000:
            raise FileToolError("每份页数应为 1～1000000 的整数。")
        if self.rotation not in (90, 180, 270):
            raise FileToolError("旋转角度应为 90、180 或 270 度。")
        if not 30 <= self.timeout <= 7200:
            raise FileToolError("单项超时应为 0.5～120 分钟。")
        if (not self.filename.lower().endswith(".pdf") or self.filename != Path(self.filename).name
                or any(char in self.filename for char in '<>:"/\\|?*') or self.filename.endswith((" ", "."))):
            raise FileToolError("合并文件名须为不含路径或非法字符的 .pdf 文件名。")


@dataclass(frozen=True)
class PdfSource:
    path: Path
    signature: tuple
    digest: str
    pages: int


@dataclass
class PdfRow:
    sources: tuple
    destinations: tuple
    selections: tuple
    options: PdfOptions
    status: str = "待处理"
    error: str = ""
    detail: str = ""
    outputs: list = field(default_factory=list)


def snapshot(path, target, stop=None):
    signature = file_signature(path)
    digest = hashlib.sha256()
    with open(path, "rb") as source, open(target, "xb") as output:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            check_stop(stop)
            digest.update(block)
            output.write(block)
    if file_signature(path) != signature:
        raise FileToolError("源文件读取期间发生变化，请重新预览。")
    return signature, digest.hexdigest()


def build_pdf_plan(paths, options, stop=None, progress=None):
    options.validate()
    sources, errors = [], []
    for index, value in enumerate(paths, 1):
        check_stop(stop)
        path = Path(value).absolute()
        try:
            if path.suffix.lower() != ".pdf":
                raise FileToolError("只支持 PDF 文件。")
            with tempfile.TemporaryDirectory(prefix="toolbox-pdf-preview-") as temp:
                work = Path(temp)
                signature, digest = snapshot(path, work / "input.pdf", stop)
                info = run_document_worker(dict(mode="pdf_info", source=str(work / "input.pdf"), password=options.password), work, stop, timeout=options.timeout)
            sources.append(PdfSource(path, signature, digest, info["pages"]))
        except FileToolCancelled:
            raise
        except Exception as exc:
            errors.append((path, str(exc)))
        if progress:
            progress(index, len(paths), path.name)
    rows = []
    if options.mode == "merge" and errors:
        listing = "；".join(f"{path.name}：{error}" for path, error in errors)
        return [PdfRow(tuple(sources), (), (), options, "无法处理", "合并包含不可用文件，未生成部分合并预览：" + listing)]
    groups = [sources] if options.mode == "merge" and sources else [[source] for source in sources]
    for group in groups:
        source = group[0]
        folder = Path(options.output_dir).expanduser().absolute() if options.output_dir else source.path.parent / "PDF处理结果"
        row = PdfRow(tuple(group), (), (), options)
        try:
            if options.mode == "merge":
                names = [options.filename]
                row.selections = tuple(tuple(range(part.pages)) for part in group)
            elif options.mode == "split":
                count = math.ceil(source.pages / options.split_every)
                names = [f"{source.path.stem}_分卷_{i + 1:03d}.pdf" for i in range(count)]
                row.selections = tuple(tuple(range(i, min(i + options.split_every, source.pages))) for i in range(0, source.pages, options.split_every))
            else:
                row.selections = (tuple(parse_pages(options.pages, source.pages)),)
                names = [source.path.stem + ("_提取.pdf" if options.mode == "extract" else "_旋转.pdf")]
            row.destinations = tuple(folder / name for name in names)
            for destination in row.destinations:
                if destination.exists() or destination.is_symlink() or key(destination) in {key(part.path) for part in group}:
                    raise FileToolError(f"输出已存在或与源文件重合，不会覆盖：{destination}")
            row.detail = f"{len(group)} 个输入，共 {sum(part.pages for part in group)} 页 → {len(names)} 个 PDF"
        except Exception as exc:
            row.status, row.error = "无法处理", str(exc)
        rows.append(row)
    for path, error in errors:
        rows.append(PdfRow((PdfSource(path, (), "", 0),), (), (), options, "无法处理", error))
    targets = {}
    for row in rows:
        if not row.error:
            for path in row.destinations:
                targets.setdefault(key(path), []).append(row)
    for group in targets.values():
        if len(group) > 1:
            for row in group:
                row.status, row.error = "无法处理", "多个任务会生成同名输出，请分批处理或更换输出目录。"
    return rows


def execute_pdf_plan(rows, options, stop=None, progress=None):
    options.validate()
    for index, row in enumerate(rows, 1):
        check_stop(stop)
        if row.status != "待处理" or row.error:
            continue
        try:
            if row.options != options:
                raise FileToolError("设置与预览不一致，请重新扫描。")
            if any(path.exists() or path.is_symlink() for path in row.destinations):
                raise FileToolError("输出目录出现同名文件，未覆盖；请重新预览。")
            folder = row.destinations[0].parent
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".toolbox-pdf-", dir=folder) as temp:
                work, inputs = Path(temp), []
                for number, source in enumerate(row.sources):
                    if file_signature(source.path) != source.signature:
                        raise FileToolError("源文件与预览不一致，请重新扫描。")
                    copy = work / f"input-{number}.pdf"
                    signature, digest = snapshot(source.path, copy, stop)
                    if signature != source.signature or digest != source.digest:
                        raise FileToolError("源文件内容已改变，请重新扫描。")
                    inputs.append(str(copy))
                outputs = [str(work / f"output-{number}.pdf") for number in range(len(row.destinations))]
                row.status = "处理中"
                result = run_document_worker(dict(mode="pdf_edit", operation=options.mode, sources=inputs, outputs=outputs, selections=row.selections,
                    rotation=options.rotation, password=options.password), work, stop,
                    (lambda text: progress(index - 1, len(rows), text)) if progress else None, options.timeout)
                for temporary, destination in zip(outputs, row.destinations):
                    check_stop(stop)
                    with open(temporary, "rb") as stream:
                        if stream.read(5) != b"%PDF-":
                            raise FileToolError("生成的 PDF 校验失败。")
                    _publish_new(Path(temporary), destination)
                    row.outputs.append(destination)
                row.status, row.detail = "已完成", result["detail"]
        except FileToolCancelled:
            row.status = "已取消"
            row.detail = f"已发布 {len(row.outputs)} 个结果，未完成的输出不发布。"
            raise
        except Exception as exc:
            row.status, row.error = "失败", str(exc)
            if row.outputs:
                row.detail = f"已发布 {len(row.outputs)} 个结果，其余未生成。"
        if progress:
            progress(index, len(rows), row.error or row.detail)
    return rows


def history_rows(rows):
    from task_history import item
    result = []
    for row in rows:
        source = "\n".join(str(value.path) for value in row.sources)
        for output in row.outputs:
            result.append(item(source, output, "已完成", row.detail))
        if not row.outputs or row.status != "已完成":
            result.append(item(source, state=row.status, detail=row.error or row.detail))
    return result
