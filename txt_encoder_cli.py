#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
txt_encoder_cli.py —— 批量文本编码转换工具（命令行版）

用法示例：
  # 1) 只看每个文件是什么编码，不做任何修改
  python txt_encoder_cli.py --detect D:\\文档

  # 2) 把整个目录（含子目录）里的 txt 全部转成 UTF-8，输出到 文档_utf8 目录
  python txt_encoder_cli.py D:\\文档 --target utf-8

  # 3) 原地把 GBK 转成 UTF-8，自动备份 .bak
  python txt_encoder_cli.py D:\\文档 --target utf-8 --mode inplace

  # 4) 先预演（不写盘），确认无误后再真正执行
  python txt_encoder_cli.py D:\\文档 --target utf-8 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from txt_encoder_core import (  # noqa: E402
    BOM_MODES,
    ENCODING_CHOICES,
    NEWLINE_MODES,
    OUTPUT_MODES,
    VERSION,
    ConvertOptions,
    EncodingNotSupported,
    canonical_codec,
    count_status,
    encoding_label,
    iter_files,
    parse_exts,
    convert_file,
    DEFAULT_EXTS,
    inspect_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="txt_encoder_cli",
        description="批量把文本文件从一种字符编码转换成另一种（支持 GBK/Big5/UTF-8/UTF-16 等）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("paths", nargs="*", help="要处理的文件或目录，可以写多个")
    parser.add_argument("-s", "--source", default="auto",
                        help="源编码，默认 auto 自动检测；也可指定 gbk / gb18030 / big5 / utf-8 等")
    parser.add_argument("-t", "--target", default="utf-8",
                        help="目标编码，默认 utf-8")
    parser.add_argument("-b", "--bom", default="keep", choices=sorted(BOM_MODES),
                        help="BOM 处理方式，默认 keep（与原文件一致）")
    parser.add_argument("-n", "--newline", default="keep", choices=sorted(NEWLINE_MODES),
                        help="换行符处理，默认 keep（保持原样）")
    parser.add_argument("-m", "--mode", default="newdir", choices=sorted(OUTPUT_MODES),
                        help="输出方式，默认 newdir（输出到新目录）")
    parser.add_argument("-o", "--out", default=None,
                        help="输出目录（mode=newdir 时生效），默认在源目录旁新建 <目录名>_<目标编码>")
    parser.add_argument("--suffix", default=None,
                        help="mode=suffix 时使用的后缀，默认根据目标编码自动生成，如 _utf8")
    parser.add_argument("-e", "--ext", default=None,
                        help="只处理这些扩展名，例如 'txt,md,log'；用 all 表示所有文件。"
                             f"默认：{','.join(sorted(DEFAULT_EXTS))}")
    parser.add_argument("-r", "--recursive", dest="recursive", action="store_true",
                        default=True, help="递归子目录（默认开启）")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="不递归子目录")
    parser.add_argument("--include-hidden", dest="skip_hidden", action="store_false",
                        default=True, help="包含以点开头的隐藏文件")
    parser.add_argument("--on-error", default="skip", choices=["skip", "replace"],
                        help="解码失败时：skip 跳过该文件（默认）/ replace 用替代字符继续")
    parser.add_argument("--on-encode-error", default="skip", choices=["skip", "replace"],
                        help="目标编码无法表示某些字符时：skip 跳过（默认）/ replace 用 ? 代替")
    parser.add_argument("--no-skip-same", dest="skip_if_same", action="store_false",
                        default=True, help="即使文件已是目标编码也重新写入")
    parser.add_argument("--no-backup", dest="backup", action="store_false", default=True,
                        help="mode=inplace 时不生成 .bak 备份（有数据风险）")
    parser.add_argument("--keep-mtime", action="store_true",
                        help="原地覆盖后保留原文件的修改时间")
    parser.add_argument("--dry-run", action="store_true",
                        help="预演：只统计和报告，不写入任何文件")
    parser.add_argument("--detect", action="store_true",
                        help="只检测并打印每个文件的编码，不做转换")
    parser.add_argument("--jobs", type=int, default=1,
                        help="并发线程数，默认 1；文件很多时可设为 4~8 提速")
    parser.add_argument("--report", default=None,
                        help="把结果写入 CSV 报告文件")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="只输出汇总，不逐条打印")
    parser.add_argument("--list-encodings", action="store_true",
                        help="列出全部支持的编码名后退出")
    parser.add_argument("-V", "--version", action="version", version=f"txt-encoder {VERSION}")
    return parser


def print_encodings() -> None:
    print("可用编码名：")
    for label, codec in ENCODING_CHOICES:
        print(f"  {codec:<12} {label}")
    print()
    print("任何 Python 认识的编码名都可以直接使用，例如 gbk, gb18030, big5hkscs, utf-16-le, cp936 ...")


def make_options(args: argparse.Namespace) -> ConvertOptions:
    try:
        canonical_codec(args.target)
        if args.source != "auto":
            canonical_codec(args.source)
    except EncodingNotSupported as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)

    suffix = args.suffix
    if suffix is None:
        from txt_encoder_core import target_tag
        suffix = "_" + target_tag(args.target, args.bom, False)

    return ConvertOptions(
        source_encoding=args.source,
        target_encoding=args.target,
        bom_mode=args.bom,
        newline=args.newline,
        on_decode_error=args.on_error,
        on_encode_error=args.on_encode_error,
        skip_if_same=args.skip_if_same,
        copy_unchanged=True,
        output_mode=args.mode,
        output_dir=Path(args.out).expanduser() if args.out else None,
        suffix=suffix,
        backup=args.backup,
        keep_mtime=args.keep_mtime,
        dry_run=args.dry_run,
    )


def run_detect(paths: list[Path], exts, recursive: bool, skip_hidden: bool) -> int:
    count = 0
    for src, _root in iter_files(paths, exts, recursive, skip_hidden=skip_hidden):
        count += 1
        try:
            info = inspect_file(src, max_chars=0)
        except OSError as exc:
            print(f"[读取失败] {src} —— {exc}")
            continue
        flag = ""
        if info.is_binary:
            flag = "  [疑似二进制]"
        elif info.is_ascii_only:
            flag = "  [纯英文]"
        print(f"{str(src)}\n    编码={info.label:<22} 置信度={info.confidence * 100:3.0f}%  "
              f"大小={info.size:>8} B  依据={info.reason}{flag}")
    if count == 0:
        print("没有找到符合条件的文件（可用 -e txt,md 指定扩展名，或用 -e all 处理全部文件）。")
        return 1
    print(f"\n共检测 {count} 个文件。")
    return 0


def run_convert(args: argparse.Namespace, options: ConvertOptions) -> int:
    paths = [Path(p).expanduser() for p in args.paths]
    for p in paths:
        if not p.exists():
            print(f"错误：路径不存在 —— {p}", file=sys.stderr)
            return 2

    exts = parse_exts(args.ext)
    exclude = []
    if options.output_dir is not None:
        exclude.append(options.output_dir)

    pairs = list(iter_files(paths, exts, args.recursive, exclude, skip_hidden=args.skip_hidden))
    total = len(pairs)
    if total == 0:
        print("没有找到符合条件的文件（可用 -e txt,md 指定扩展名，或用 -e all 处理全部文件）。")
        return 1

    label = encoding_label(options.target_encoding)
    action = "预演" if options.dry_run else "转换"
    print(f"开始{action}：{total} 个文件 → {label}"
          f"（源编码：{encoding_label(options.source_encoding)}，"
          f"输出方式：{OUTPUT_MODES.get(options.output_mode, options.output_mode)}）")

    results = []
    done = 0
    width = len(str(total))

    def handle(result) -> None:
        nonlocal done
        done += 1
        results.append(result)
        if args.quiet:
            return
        mark = {"converted": "√", "copied": "·", "unchanged": "·"}.get(result.status, "×")
        print(f"[{done:>{width}}/{total}] {mark} {result.summary()}")

    if args.jobs and args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for result in pool.map(lambda pair: convert_file(pair[0], options, pair[1]), pairs):
                handle(result)
    else:
        for src, root in pairs:
            handle(convert_file(src, options, root))

    stats = count_status(results)
    failed = [r for r in results if r.status == "failed"]

    print("\n—— 汇总 ——")
    for key, text in (("converted", "已转换"), ("copied", "已复制（本来就无需转换）"),
                      ("unchanged", "已在目标编码，跳过"), ("skipped", "已跳过"),
                      ("failed", "失败"), ("preview", "待转换（预演）")):
        if stats.get(key):
            print(f"  {text}: {stats[key]}")

    if failed:
        print("\n以下文件未能处理：")
        for item in failed[:50]:
            print(f"  - {item.src}\n      {item.error}")
        if len(failed) > 50:
            print(f"  ... 另有 {len(failed) - 50} 个，详见报告文件")

    if args.report:
        write_report(Path(args.report).expanduser(), results)
        print(f"\n报告已写入：{args.report}")

    if options.dry_run:
        print("\n这是预演，未写入任何文件。去掉 --dry-run 即可真正执行。")

    return 1 if failed else 0


def write_report(path: Path, results) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["源文件", "输出文件", "状态", "源编码", "置信度", "目标编码",
                         "原大小", "新大小", "说明", "错误"])
        for item in results:
            writer.writerow([
                str(item.src),
                str(item.dst) if item.dst else "",
                item.status_text(),
                item.source_label or item.source_encoding,
                f"{item.confidence:.2f}",
                encoding_label(item.target_encoding),
                item.size_before,
                item.size_after,
                item.reason,
                item.error,
            ])


def main(argv=None) -> int:
    _setup_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_encodings:
        print_encodings()
        return 0

    if not args.paths:
        parser.print_help()
        return 1

    paths = [Path(p).expanduser() for p in args.paths]
    exts = parse_exts(args.ext)

    if args.detect:
        return run_detect(paths, exts, args.recursive, args.skip_hidden)

    options = make_options(args)

    if options.output_mode == "inplace" and not options.dry_run and not options.backup:
        print("警告：原地覆盖且未备份，原文件内容将被替换。", file=sys.stderr)
    if options.output_mode == "inplace" and options.output_dir is not None:
        print("提示：mode=inplace 时 --out 会被忽略。")

    return run_convert(args, options)


def _setup_console_encoding() -> None:
    """让 Windows 控制台也能正常输出中文（cp936 终端下不再报 UnicodeEncodeError）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        raise SystemExit(130)
