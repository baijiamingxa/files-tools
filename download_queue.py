#!/usr/bin/env python3
"""可恢复的顺序下载队列；实际下载继续由 DownloadJob 完成。"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from txt_encoder_core import decode_bytes


def parse_links(text):
    links = []
    for number, line in enumerate(text.splitlines(), 1):
        value = line.strip().lstrip("\ufeff")
        if not value or value.startswith("#"):
            continue
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError(f"第 {number} 行不是完整的 HTTP / HTTPS 链接。")
        links.append(value)
    return list(dict.fromkeys(links))


def import_links(path):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("链接 TXT 超过 4 MB，请分批导入。")
    text, _, _ = decode_bytes(path.read_bytes())
    return parse_links(text)


@dataclass
class QueueItem:
    url: str
    directory: str
    threads: int = 8
    identity: str = ""
    path: str = ""
    status: str = "等待"
    error: str = ""

    def __post_init__(self):
        if not self.identity:
            self.identity = uuid.uuid4().hex


def save_queue(items, path):
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_queue(path):
    if path is None or not Path(path).is_file():
        return []
    path = Path(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("队列配置过大，未自动载入。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) > 5000:
        raise ValueError("队列配置格式或数量不正确。")
    items, identities = [], set()
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("队列记录格式不正确。")
        item = QueueItem(**row)
        if not isinstance(item.identity, str) or not item.identity.isalnum() or item.identity in identities:
            raise ValueError("队列任务编号无效。")
        if not isinstance(item.url, str) or parse_links(item.url) != [item.url]:
            raise ValueError("队列含无效链接。")
        if not isinstance(item.directory, str) or not Path(item.directory).is_absolute() or not isinstance(item.threads, int) or not 1 <= item.threads <= 64:
            raise ValueError("队列目录或线程数无效。")
        if not isinstance(item.path, str) or (item.path and Path(item.path).parent != Path(item.directory)):
            raise ValueError("队列保存路径不属于指定目录。")
        if item.status not in ("等待", "下载中", "已暂停", "失败", "完成"):
            raise ValueError("队列任务状态无效。")
        if item.status == "下载中":
            item.status = "已暂停"
        identities.add(item.identity)
        items.append(item)
    return items
