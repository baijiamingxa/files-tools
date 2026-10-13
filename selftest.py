#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— 自检脚本：构造各种编码的样例文件，验证检测与转换是否正确。

运行：python selftest.py
全部通过会打印 “全部通过”。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txt_encoder_core import (  # noqa: E402
    ConvertOptions,
    convert_file,
    detect_bytes,
    detect_file,
    encode_text,
    make_console_tolerant,
)

SIMPLIFIED = "你好，世界！这是一段简体中文测试文本。\r\n第二行：数字 123，符号 @#$。\r\n"
ENGLISH = "Hello world, this is a plain ASCII text file.\nSecond line.\n"
TRADITIONAL = "繁體中文測試：這是一段文字，用來驗證 Big5 編碼轉換。\n"

# (文件后缀, 编码, 是否写 BOM, 允许被识别成这些编码之一)
ENCODING_CASES = [
    ("utf8", "utf-8", False, {"utf-8"}),
    ("utf8bom", "utf-8", True, {"utf-8-sig"}),
    ("utf16le", "utf-16-le", True, {"utf-16-le"}),
    ("utf16be", "utf-16-be", True, {"utf-16-be"}),
    ("gbk", "gbk", False, {"gbk", "gb2312"}),
    ("gb2312", "gb2312", False, {"gb2312"}),
    ("gb18030", "gb18030", False, {"gb18030", "gbk", "gb2312"}),
]

failures: list[str] = []


def check(condition: bool, title: str, detail: str = "") -> None:
    if condition:
        print(f"  [通过] {title}")
    else:
        print(f"  [失败] {title}   {detail}")
        failures.append(title)


def make_file(path: Path, text: str, encoding: str, bom: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode(encoding)
    if bom:
        data, _ = encode_text(text, encoding, "add")
    path.write_bytes(data)


def test_detection(tmp: Path) -> None:
    print("\n[1] 编码检测")
    for suffix, codec, bom, expected in ENCODING_CASES:
        for name, text in (("中文", SIMPLIFIED), ("英文", ENGLISH)):
            path = tmp / "detect" / f"{name}_{suffix}.txt"
            try:
                make_file(path, text, codec, bom)
            except UnicodeEncodeError:
                continue
            result = detect_file(path)
            ok = result.encoding in expected
            # 纯英文内容被判成 UTF-8 是正确结果
            if name == "英文" and result.encoding == "utf-8":
                ok = True
            check(ok, f"{name}_{suffix}.txt → {result.label}（{result.confidence * 100:.0f}%）",
                  f"期望 {sorted(expected)}，实际 {result.encoding}")

    trad = tmp / "detect" / "繁体_big5.txt"
    make_file(trad, TRADITIONAL, "big5", False)
    result = detect_file(trad)
    check(result.encoding == "big5", f"繁体 Big5 → {result.label}（{result.confidence * 100:.0f}%）",
          f"实际 {result.encoding}")

    no_bom_utf16 = tmp / "detect" / "英文_utf16无BOM.txt"
    make_file(no_bom_utf16, ENGLISH, "utf-16-le", False)
    result = detect_file(no_bom_utf16)
    check(result.encoding == "utf-16-le",
          f"无 BOM 的 UTF-16 LE → {result.label}（{result.confidence * 100:.0f}%）",
          f"实际 {result.encoding}")

    check(detect_bytes(b"").encoding == "utf-8", "空文件不报错")


def test_to_utf8(tmp: Path) -> None:
    print("\n[2] 各种编码 → UTF-8，内容必须逐字一致")
    text = SIMPLIFIED
    for suffix, codec, bom, _expected in ENCODING_CASES:
        src = tmp / "to_utf8" / f"sample_{suffix}.txt"
        make_file(src, text, codec, bom)
        options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                                 bom_mode="none", output_mode="newdir")
        result = convert_file(src, options, src.parent)
        if result.status == "failed":
            check(False, f"{suffix} → UTF-8", result.error)
            continue
        got = result.dst.read_bytes().decode("utf-8")
        check(got == text, f"{suffix} → UTF-8 内容一致",
              f"识别为 {result.source_label}")


def test_from_utf8(tmp: Path) -> None:
    print("\n[3] UTF-8 → 其他编码")
    text = "中文转码测试：ABC 123。\n"
    src = tmp / "from_utf8" / "source_utf8.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(text.encode("utf-8"))

    cases = [
        ("gbk", "none", b"", "gbk"),
        ("utf-16-le", "add", b"\xff\xfe", "utf-16-le"),
        ("utf-8-sig", "default", b"\xef\xbb\xbf", "utf-8"),
        ("gb18030", "none", b"", "gb18030"),
        ("utf-8", "keep", b"", "utf-8"),
    ]
    for target, bom_mode, bom_prefix, decode_with in cases:
        options = ConvertOptions(source_encoding="auto", target_encoding=target,
                                 bom_mode=bom_mode, output_mode="newdir")
        result = convert_file(src, options, src.parent)
        if result.status == "failed":
            check(False, f"UTF-8 → {target}", result.error)
            continue
        raw = result.dst.read_bytes()
        if bom_prefix:
            ok_bom = raw.startswith(bom_prefix)
            check(ok_bom, f"UTF-8 → {target} 正确写入 BOM", raw[:6].hex())
            raw = raw[len(bom_prefix):]
        check(raw.decode(decode_with) == text, f"UTF-8 → {target} 内容一致",
              f"状态 {result.status}")

    trad = tmp / "from_utf8" / "trad.txt"
    trad.write_bytes(TRADITIONAL.encode("utf-8"))
    options = ConvertOptions(source_encoding="auto", target_encoding="big5",
                             bom_mode="none", output_mode="newdir")
    result = convert_file(trad, options, trad.parent)
    check(result.status == "converted" and result.dst.read_bytes().decode("big5") == TRADITIONAL,
          "UTF-8 → Big5 内容一致", result.error)

    options = ConvertOptions(source_encoding="auto", target_encoding="ascii",
                             on_encode_error="skip", output_mode="newdir")
    result = convert_file(src, options, src.parent)
    check(result.status == "failed" and "无法表示" in result.error,
          "UTF-8 → ASCII 遇中文时明确报错而不是写乱码", result.error)


def test_modes(tmp: Path) -> None:
    print("\n[4] 换行符 / 原地覆盖 / 跳过已符合 / 预演")
    src = tmp / "modes" / "crlf.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes("第一行\r\n第二行\r\n".encode("gbk"))
    options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                             newline="lf", output_mode="newdir")
    result = convert_file(src, options, src.parent)
    check(result.status == "converted" and b"\r" not in result.dst.read_bytes(),
          "GBK → UTF-8 同时把 CRLF 统一成 LF")

    inplace_src = tmp / "modes" / "inplace.txt"
    inplace_src.write_bytes("原地覆盖测试。\n".encode("gbk"))
    options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                             output_mode="inplace", backup=True)
    result = convert_file(inplace_src, options, inplace_src.parent)
    backup = inplace_src.with_name(inplace_src.name + ".bak")
    check(result.status == "converted"
          and inplace_src.read_bytes() == "原地覆盖测试。\n".encode("utf-8"),
          "原地覆盖后文件变成 UTF-8")
    check(backup.exists() and backup.read_bytes() == "原地覆盖测试。\n".encode("gbk"),
          "原地覆盖前生成了 .bak 备份（内容为原编码）")

    options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                             output_mode="inplace")
    result = convert_file(inplace_src, options, inplace_src.parent)
    check(result.status == "unchanged", "已经是 UTF-8 的文件被跳过", result.status)

    dry_src = tmp / "modes" / "dryrun.txt"
    dry_src.write_bytes("预演不写盘测试。\n".encode("gbk"))
    options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                             output_mode="inplace", dry_run=True)
    result = convert_file(dry_src, options, dry_src.parent)
    check(result.status == "preview"
          and dry_src.read_bytes() == "预演不写盘测试。\n".encode("gbk"),
          "预演模式完全不修改文件")


def test_robustness(tmp: Path) -> None:
    print("\n[5] 二进制文件与异常情况")
    binary = tmp / "misc" / "image.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(bytes(range(256)) * 8)
    result = convert_file(binary, ConvertOptions(output_mode="inplace"), binary.parent)
    check(result.status == "skipped", "二进制文件被自动跳过", result.status)

    missing = tmp / "misc" / "不存在.txt"
    result = convert_file(missing, ConvertOptions())
    check(result.status == "failed" and "读取失败" in result.error,
          "文件不存在时返回失败信息而不是抛异常")

    wrong = tmp / "misc" / "gbk文件按utf8读.txt"
    wrong.write_bytes("这是 GBK 编码的内容。\n".encode("gbk"))
    options = ConvertOptions(source_encoding="utf-8", target_encoding="utf-8",
                             on_decode_error="skip", output_mode="inplace")
    result = convert_file(wrong, options, wrong.parent)
    check(result.status == "failed" and "解码失败" in result.error,
          "指定了错误的源编码时会明确报错并保持原文件不变",
          result.status + " " + result.error)
    check(wrong.read_bytes() == "这是 GBK 编码的内容。\n".encode("gbk"),
          "解码失败的文件内容没有被破坏")


def test_layout(tmp: Path) -> None:
    print("\n[6] 输出目录结构与后缀模式")
    root = tmp / "tree"
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes("根目录文件。\n".encode("gbk"))
    (root / "sub" / "b.txt").write_bytes("子目录文件。\n".encode("gbk"))

    options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                             output_mode="newdir")
    ra = convert_file(root / "a.txt", options, root)
    rb = convert_file(root / "sub" / "b.txt", options, root)
    check(ra.dst == root.parent / "tree_utf8" / "a.txt",
          "输出到新目录：根文件路径正确", str(ra.dst))
    check(rb.dst == root.parent / "tree_utf8" / "sub" / "b.txt",
          "输出到新目录：保留子目录结构", str(rb.dst))
    check((root / "a.txt").read_bytes() == "根目录文件。\n".encode("gbk"),
          "输出到新目录：原文件未被改动")

    options = ConvertOptions(source_encoding="auto", target_encoding="utf-8",
                             output_mode="suffix", suffix="_utf8")
    rc = convert_file(root / "a.txt", options, root)
    check(rc.dst == root / "a_utf8.txt", "后缀模式生成 a_utf8.txt", str(rc.dst))


def main() -> int:
    make_console_tolerant()
    tmp = Path(tempfile.mkdtemp(prefix="txtenc_selftest_"))
    print("=" * 64)
    print("txt-encoder 自检开始")
    print(f"临时目录：{tmp}")
    print("=" * 64)
    try:
        test_detection(tmp)
        test_to_utf8(tmp)
        test_from_utf8(tmp)
        test_modes(tmp)
        test_robustness(tmp)
        test_layout(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 64)
    if failures:
        print(f"有 {len(failures)} 项未通过：")
        for item in failures:
            print("  -", item)
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
