#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_sample_files.py —— 生成一批各种编码的示例文本，用来试用/验证本工具。

用法：python make_sample_files.py [输出目录]
默认输出到脚本同级的 “示例文件” 目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txt_encoder_core import encode_text, make_console_tolerant  # noqa: E402

SIMPLIFIED = (
    "第一行：这是一段简体中文 GBK 测试文本。\r\n"
    "第二行：数字 123456，符号 @#$%%，标点，。！？\r\n"
    "第三行：混合 ABCdef 内容。\r\n"
)

TRADITIONAL = (
    "第一行：這是一段繁體中文 Big5 測試文字。\r\n"
    "第二行：數字 123456，符號 @#$%%。\r\n"
)

ENGLISH = "line one: plain ascii\nline two: 12345\n"


def main() -> int:
    make_console_tolerant()
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "示例文件"
    root.mkdir(parents=True, exist_ok=True)
    sub = root / "子目录"
    sub.mkdir(exist_ok=True)

    files: list[tuple[Path, bytes]] = [
        (root / "简体_GBK.txt", SIMPLIFIED.encode("gbk")),
        (root / "简体_GB2312.txt", SIMPLIFIED.encode("gb2312")),
        (root / "简体_GB18030.txt", SIMPLIFIED.encode("gb18030")),
        (root / "简体_UTF8.txt", SIMPLIFIED.encode("utf-8")),
        (root / "简体_UTF8带BOM.txt", encode_text(SIMPLIFIED, "utf-8-sig", "add")[0]),
        (root / "简体_UTF16带BOM.txt", encode_text(SIMPLIFIED, "utf-16-le", "add")[0]),
        (root / "繁体_Big5.txt", TRADITIONAL.encode("big5")),
        (root / "纯英文_ASCII.txt", ENGLISH.encode("ascii")),
        (root / "纯英文_UTF16无BOM.txt", ENGLISH.encode("utf-16-le")),
        (sub / "嵌套_GBK.txt", SIMPLIFIED.encode("gbk")),
        (sub / "嵌套_UTF8.txt", SIMPLIFIED.encode("utf-8")),
        (root / "不是文本.bin", bytes(range(256)) * 4),
    ]

    for path, data in files:
        path.write_bytes(data)
        print(f"  写入 {path.relative_to(root)}  ({len(data)} 字节)")

    print(f"\n共生成 {len(files)} 个示例文件，目录：{root}")
    print("可以用本工具扫描这个目录，看看能否正确识别每一种编码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
