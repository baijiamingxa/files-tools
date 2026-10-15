#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
txt_encoder_core.py —— 文本文件编码检测与批量转码核心引擎

仅依赖 Python 标准库，无需 pip 安装任何东西。

能力：
  * 自动识别：UTF-8(带/不带 BOM)、UTF-16/UTF-32(带 BOM)、UTF-16 无 BOM、
    GB2312 / GBK / GB18030、Big5 / Big5-HKSCS、Shift-JIS、EUC-KR、
    Windows-1252(ANSI)、Latin-1 等
  * 批量转码，三种输出方式：输出到新目录 / 同目录加后缀 / 原地覆盖(自动备份)
  * 可选 BOM 策略、换行符统一、跳过已是目标编码的文件、解码失败处理
  * 分块流式读写：内存占用与文件大小无关，几百 MB / 几 GB 的大文件也能转
  * 原子写入（先写临时文件再替换），中断不会写出半个文件
"""

from __future__ import annotations

import codecs
import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field, replace as _replace
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

VERSION = "1.0.0"

# 编码清单（界面 / 命令行共用）

# (显示名称, Python 编码名)
ENCODING_CHOICES: list[tuple[str, str]] = [
    ("自动检测", "auto"),
    ("UTF-8", "utf-8"),
    ("UTF-8 (带 BOM)", "utf-8-sig"),
    ("UTF-16 (带 BOM)", "utf-16"),
    ("UTF-16 LE", "utf-16-le"),
    ("UTF-16 BE", "utf-16-be"),
    ("GBK (兼容 GB2312)", "gbk"),
    ("GB2312", "gb2312"),
    ("GB18030", "gb18030"),
    ("Big5 (繁体中文)", "big5"),
    ("Big5-HKSCS (香港繁体)", "big5hkscs"),
    ("Shift-JIS (日文)", "shift_jis"),
    ("EUC-KR (韩文)", "euc_kr"),
    ("Windows-1252 / ANSI", "cp1252"),
    ("Latin-1 / ISO-8859-1", "latin-1"),
    ("ASCII (纯英文)", "ascii"),
]

# 目标编码下拉框里真正适合作为“输出”的编码
TARGET_CHOICES: list[tuple[str, str]] = [
    ("UTF-8（推荐，跨平台通用）", "utf-8"),
    ("UTF-8（带 BOM，Excel/旧版记事本友好）", "utf-8-sig"),
    ("UTF-16 LE（Windows 记事本“Unicode”）", "utf-16-le"),
    ("UTF-16 BE（带 BOM）", "utf-16-be"),
    ("GBK（Windows 记事本“ANSI”，简体中文）", "gbk"),
    ("GB18030（国标全字符集）", "gb18030"),
    ("GB2312（老国标）", "gb2312"),
    ("Big5（繁体中文）", "big5"),
    ("Big5-HKSCS（香港繁体）", "big5hkscs"),
    ("Shift-JIS（日文）", "shift_jis"),
    ("EUC-KR（韩文）", "euc_kr"),
    ("Windows-1252（西欧 ANSI）", "cp1252"),
    ("Latin-1 / ISO-8859-1", "latin-1"),
    ("ASCII（纯英文，遇中文会失败）", "ascii"),
]

ENCODING_LABELS: dict[str, str] = {}
for _label, _codec in list(ENCODING_CHOICES) + list(TARGET_CHOICES):
    ENCODING_LABELS.setdefault(_codec, _label)

LABEL_TO_CODEC = {label: codec for label, codec in list(ENCODING_CHOICES) + list(TARGET_CHOICES)}


def encoding_label(codec: str) -> str:
    """把编码名换成人类可读的中文名称。"""
    if not codec or codec.lower() == "auto":
        return "自动检测"
    key = codec.lower().replace("_", "-")
    if key in ENCODING_LABELS:
        return ENCODING_LABELS[key]
    try:
        canonical = canonical_codec(codec)
    except ValueError:
        return codec
    return ENCODING_LABELS.get(canonical, canonical)


# BOM 表：顺序很重要，UTF-32 的 BOM 前缀与 UTF-16 相同，必须先匹配
BOM_ENTRIES: list[tuple[bytes, str, str]] = [
    (codecs.BOM_UTF32_LE, "utf-32-le", "UTF-32 LE"),
    (codecs.BOM_UTF32_BE, "utf-32-be", "UTF-32 BE"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le", "UTF-16 LE"),
    (codecs.BOM_UTF16_BE, "utf-16-be", "UTF-16 BE"),
]

BOM_BYTES: dict[str, bytes] = {
    "utf-8": codecs.BOM_UTF8,
    "utf-16-le": codecs.BOM_UTF16_LE,
    "utf-16-be": codecs.BOM_UTF16_BE,
    "utf-32-le": codecs.BOM_UTF32_LE,
    "utf-32-be": codecs.BOM_UTF32_BE,
}

# 默认参与扫描的文件扩展名
DEFAULT_EXTS: tuple[str, ...] = (
    ".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".log", ".json",
    ".ini", ".cfg", ".conf", ".srt", ".vtt", ".ass", ".xml", ".html", ".htm",
    ".yaml", ".yml", ".bat", ".cmd", ".ps1", ".reg", ".sql", ".c", ".h",
    ".cpp", ".cs", ".java", ".py", ".js", ".css", ".php", ".lrc", ".nfo",
)

DETECT_SAMPLE_BYTES = 1024 * 1024
SCORE_SAMPLE_CHARS = 200_000

# 流式转码的块大小：读写都是按这个大小分块，内存占用与文件大小无关
CHUNK_SIZE = 1024 * 1024

# 预览内容只读文件开头这么点，不碰整文件
PREVIEW_SAMPLE_BYTES = 256 * 1024

# 同一组里的编码，对同一段文本写出来的字节完全一致。
# 组内第二个数字是“宽度”：目标不比源窄时，转码是字节恒等的，可以不做任何写盘。
_BYTE_IDENTITY_GROUPS: dict[str, tuple[str, int]] = {
    "utf-8": ("utf8", 1),
    "utf-8-sig": ("utf8", 1),
    "gb2312": ("gb", 0),
    "gbk": ("gb", 1),
    "gb18030": ("gb", 2),
    "big5": ("big5", 0),
    "big5hkscs": ("big5", 1),
}

# 纯 ASCII 内容在这些编码里字节完全一样（UTF-16 / UTF-32 之类显然不算）
_ASCII_SAFE_CODECS = frozenset({
    "utf-8", "utf-8-sig", "ascii", "gb2312", "gbk", "gb18030",
    "big5", "big5hkscs", "iso8859-1", "cp1252",
})


def codec_may_keep_bytes(source_codec: str, target_codec: str) -> bool:
    """
    同一段文本用 target_codec 写出来，字节是否**可能**和源文件一模一样。
    只在这时才有必要做逐字节比对；也是“已经是目标编码，不用动”的快速判据。
    """
    if source_codec == target_codec:
        return True
    source = _BYTE_IDENTITY_GROUPS.get(source_codec)
    target = _BYTE_IDENTITY_GROUPS.get(target_codec)
    return bool(source and target and source[0] == target[0] and target[1] >= source[1])


class EncodingNotSupported(ValueError):
    """编码名无效。"""


class DecodeFailure(Exception):
    """转码过程中解码失败，带上级位置信息（文件的第几个字节附近）。"""

    def __init__(self, offset: int, original: UnicodeDecodeError) -> None:
        super().__init__(str(original))
        self.offset = offset
        self.original = original


def make_console_tolerant() -> None:
    """
    让中文在“非 UTF-8 控制台 / 被重定向的管道”里也能打印：
    遇到无法表示的字符退化成 ? 而不是直接抛 UnicodeEncodeError 把程序打断。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# 基础工具

def canonical_codec(name: str) -> str:
    """把编码别名规范成 Python 标准名，例如 LATIN-1 -> iso8859-1。"""
    if name is None:
        raise EncodingNotSupported("编码名不能为空")
    key = str(name).strip()
    if not key:
        raise EncodingNotSupported("编码名不能为空")
    try:
        return codecs.lookup(key).name
    except LookupError as exc:
        raise EncodingNotSupported(f"不支持的编码名：{name}") from exc


def strip_leading_bom(text: str) -> str:
    """去掉解码后残留的 BOM 字符（U+FEFF）。"""
    if text and text[0] == "\ufeff":
        return text[1:]
    return text


def _control_ratio(text: str, sample: int = SCORE_SAMPLE_CHARS) -> float:
    """控制字符 / 替换字符占比，用来判断解码结果是否像正常文本。"""
    chunk = text[:sample]
    if not chunk:
        return 0.0
    bad = 0
    for ch in chunk:
        o = ord(ch)
        if o < 0x20 and ch not in "\t\n\v\f\r":
            bad += 1
        elif o == 0x7F or 0x80 <= o <= 0x9F or ch == "\ufffd":
            bad += 1
    return bad / len(chunk)


def _char_score(ch: str) -> float:
    """单个字符“像正常文本”的权重。正值=常见，负值=乱码特征。"""
    o = ord(ch)
    if ch in "\t\n\v\f\r":
        return 0.0
    if o < 0x20 or o == 0x7F:
        return -6.0
    if o < 0x7F:
        return 0.4
    if 0x80 <= o <= 0x9F:
        return -5.0
    if o in (0x00A0, 0x00AD):
        return 0.2
    if 0x00A1 <= o <= 0x02FF:
        return 0.5
    if 0x2000 <= o <= 0x206F:
        return 1.0
    if 0x2070 <= o <= 0x2BFF:
        return 0.5
    if 0x3000 <= o <= 0x303F:
        return 2.0
    if 0x3040 <= o <= 0x30FF:
        return 1.5
    if 0x3100 <= o <= 0x33FF:
        return 1.0
    if 0x3400 <= o <= 0x4DBF:
        return 0.6
    if 0x4E00 <= o <= 0x9FFF:
        return 3.0
    if 0xAC00 <= o <= 0xD7AF:
        return 1.5
    if 0xE000 <= o <= 0xF8FF:
        return -6.0
    if 0xF900 <= o <= 0xFAFF:
        return 0.6
    if 0xFE00 <= o <= 0xFEFF:
        return 0.8
    if 0xFF00 <= o <= 0xFFEF:
        return 1.2
    if o >= 0x20000:
        return -3.0
    category = unicodedata.category(ch)
    if category[0] == "C":
        return -4.0
    return 0.0


def _score_text(text: str) -> float:
    """文本可读程度的平均分，越高越像正确解码。"""
    chunk = text[:SCORE_SAMPLE_CHARS]
    if not chunk:
        return 0.0
    total = 0.0
    for ch in chunk:
        total += _char_score(ch)
    return total / len(chunk)


def _guess_utf16_no_bom(data: bytes) -> Optional[tuple[str, float]]:
    """无 BOM 的 UTF-16：靠 NUL 字节的位置分布判断字节序。"""
    usable = len(data) - (len(data) % 2)
    if usable < 8:
        return None
    sample = data[: min(usable, 512 * 1024)]
    sample = sample[: len(sample) - (len(sample) % 2)]
    half = len(sample) // 2
    if half == 0:
        return None
    even_nul = sample[0::2].count(0) / half
    odd_nul = sample[1::2].count(0) / half
    if odd_nul >= 0.30 and even_nul <= 0.02:
        return "utf-16-le", min(0.98, 0.60 + odd_nul * 0.35)
    if even_nul >= 0.30 and odd_nul <= 0.02:
        return "utf-16-be", min(0.98, 0.60 + even_nul * 0.35)
    return None


def looks_binary(data: bytes) -> bool:
    """粗判是不是二进制文件（图片、压缩包等），避免误转码。"""
    if not data:
        return False
    sample = data[:8192]
    if _starts_with_bom(sample):
        return False  # 带 BOM 的一定是文本（UTF-8/16/32 的 BOM 表）
    if b"\x00" in sample:
        # 有 NUL 字节：符合 UTF-16 特征就是文本，否则当二进制处理
        return _guess_utf16_no_bom(sample) is None
    text_bytes = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D, 0x0C, 0x0B, 0x08, 0x1B}
    weird = sum(1 for b in sample if b < 0x80 and b not in text_bytes)
    return weird / len(sample) > 0.30


def _double_byte_stats(data: bytes) -> tuple[float, int]:
    """
    统计双字节字符的尾字节分布，用来区分 GB 系与 Big5。

    简体/GB 系文本（GB2312 子集）的尾字节几乎全部落在 0xA1~0xFE；
    繁体 Big5 文本约有 40%~60% 的尾字节落在 0x40~0x7E（ASCII 字母区）。
    返回 (低位尾字节占比, 双字节总数)。
    """
    low = 0
    high = 0
    index = 0
    size = len(data)
    while index < size - 1:
        lead = data[index]
        if lead >= 0x81:
            trail = data[index + 1]
            if 0x40 <= trail <= 0x7E:
                low += 1
            elif 0xA1 <= trail <= 0xFE:
                high += 1
            index += 2
        else:
            index += 1
    total = low + high
    if total == 0:
        return 0.0, 0
    return low / total, total


BYTE_STRUCTURE_WEIGHT = 1.2


def _byte_structure_bonus(codec: str, low_ratio: float, pair_count: int) -> float:
    """按尾字节分布给 GB 系 / Big5 候选加分。"""
    if pair_count < 8:
        return 0.0
    if codec in ("gb18030", "gbk", "gb2312"):
        return BYTE_STRUCTURE_WEIGHT * (1.0 - low_ratio)
    if codec in ("big5", "big5hkscs"):
        return BYTE_STRUCTURE_WEIGHT * low_ratio
    return 0.0


# 编码检测

@dataclass
class DetectResult:
    """一次编码检测的结论。"""

    encoding: str
    confidence: float
    reason: str = ""
    has_bom: bool = False
    bom_name: str = ""
    is_ascii_only: bool = False
    candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return encoding_label(self.encoding)

    @property
    def confidence_pct(self) -> str:
        return f"{self.confidence * 100:.0f}%"

    def describe(self) -> str:
        bits = [f"{self.label}（{self.confidence_pct}）"]
        if self.reason:
            bits.append(self.reason)
        return " · ".join(bits)


def detect_bytes(data: bytes) -> DetectResult:
    """检测字节流的编码。纯标准库启发式算法，无需第三方库。"""
    if not data:
        return DetectResult("utf-8", 1.0, "空文件，按 UTF-8 处理", is_ascii_only=True)

    sample = data[:DETECT_SAMPLE_BYTES]

    # 1) BOM 最可靠
    for bom, codec, name in BOM_ENTRIES:
        if sample.startswith(bom):
            return DetectResult(
                codec, 0.99, f"文件头带 {name} BOM 标记",
                has_bom=True, bom_name=name,
            )

    # 2) 无 BOM 的 UTF-16（大量 NUL 字节）
    utf16 = _guess_utf16_no_bom(sample)
    if utf16:
        codec, conf = utf16
        return DetectResult(codec, conf, f"无 BOM，但字节分布符合 {codec.upper()} 特征")

    # 3) 纯 ASCII
    if all(b < 0x80 for b in sample):
        return DetectResult("utf-8", 1.0, "纯 ASCII 字符，UTF-8 / GBK 通用", is_ascii_only=True)

    # 4) 多候选打分对比（UTF-8 解出来的是真文本，但 UTF-16 无 BOM 等情况也可能
    #    被 UTF-8 误判，所以这里统一打分，只给 UTF-8 一点点先验加分）
    scored: list[tuple[str, float, str]] = []
    candidates = [
        "utf-8", "gb18030", "gbk", "big5", "big5hkscs", "shift_jis", "euc_kr",
        "cp1252", "latin-1",
    ]
    low_ratio, pair_count = _double_byte_stats(sample)
    for codec in candidates:
        try:
            decoded = sample.decode(codec)
        except UnicodeDecodeError:
            continue
        if "\x00" in decoded:
            continue
        if codec == "utf-8" and _control_ratio(decoded) > 0.01:
            continue
        score = _score_text(decoded)
        if codec == "utf-8":
            score += 0.35
        score += _byte_structure_bonus(codec, low_ratio, pair_count)
        scored.append((codec, score, decoded))

    if len(sample) % 2 == 0:
        has_nul = b"\x00" in sample
        for codec in ("utf-16-le", "utf-16-be"):
            try:
                decoded = sample.decode(codec)
            except UnicodeDecodeError:
                continue
            penalty = 0.0 if has_nul else 0.9
            scored.append((codec, _score_text(decoded) - penalty, decoded))

    # 不同编码解出完全相同文本时只保留一个（例如 GBK 与 GB18030 对同一批字节）
    unique: list[tuple[str, float, str]] = []
    seen_texts: set[str] = set()
    for codec, score, text in scored:
        if text in seen_texts:
            continue
        seen_texts.add(text)
        unique.append((codec, score, text))
    scored = unique

    if not scored:
        return DetectResult("cp1252", 0.25, "无法确定编码，暂按 Windows-1252 处理")

    scored.sort(key=lambda item: item[1], reverse=True)
    best_codec, best_score, _best_text = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else -99.0
    margin = best_score - second_score
    confidence = max(0.35, min(0.95, 0.45 + margin * 0.45))

    reason = "按字符分布打分选出"
    if best_score < 0.25:
        confidence = min(confidence, 0.4)
        reason = "文本特征不明显，建议人工确认"

    # 能收窄到更常见的窄编码就收窄（GB2312 ⊂ GBK ⊂ GB18030）
    if best_codec in ("gb18030", "gbk"):
        best_codec, reason = _narrow_gb_codec(sample, best_codec, reason)
    elif best_codec in ("big5", "big5hkscs"):
        try:
            if sample.decode("big5").encode("big5") == sample:
                best_codec = "big5"
        except UnicodeDecodeError:
            pass

    if margin < 0.35 and len(scored) > 1 and best_score > 0.25:
        confidence = min(confidence, 0.6)
        other = scored[1][0]
        reason = f"与 {encoding_label(other)} 的得分接近，请核对预览后确认"

    return DetectResult(
        best_codec, confidence, reason,
        candidates=[(c, round(s, 3)) for c, s, _ in scored],
    )


def _narrow_gb_codec(data: bytes, codec: str, reason: str) -> tuple[str, str]:
    """GB18030/GBK 能收窄成更常见的编码时返回更具体的名字。"""
    for narrow in ("gb2312", "gbk"):
        try:
            if data.decode(narrow).encode(narrow) == data:
                return narrow, "国标编码，字节可被 " + narrow.upper() + " 完全表示"
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return codec, reason


def _is_all_ascii(data: bytes) -> bool:
    return max(data, default=0) < 0x80


def _probe_clean_block(fh, offset: int, length: int) -> bytes:
    """
    从文件中间取一段“干净”的字节：以换行符开头、以换行符结尾。

    换行字节 0x0A 不会出现在 UTF-8 / GBK / Big5 的多字节序列内部，
    所以按换行切出来的块一定是完整字符，不会因为截断把编码检测带偏。
    """
    fh.seek(offset)
    block = fh.read(length)
    start = block.find(b"\n")
    if start == -1:
        return b""
    end = block.rfind(b"\n")
    if end <= start:
        return b""
    return block[start + 1:end + 1]


def sample_file_bytes(path: os.PathLike | str, limit: int = DETECT_SAMPLE_BYTES) -> tuple[bytes, str]:
    """
    取样用于编码检测，最多读 limit 字节，返回 (样本, 取样位置说明)。

    只看开头是不够的：日志类文件常见“英文表头 + 中文正文”，
    所以开头如果是纯 ASCII，再分别抽中段和末段各一段来判断。
    整个过程只读几段样本，多大的文件都不会把内存吃满。
    """
    p = Path(path)
    size = p.stat().st_size
    with p.open("rb") as fh:
        if size <= limit:
            return fh.read(size), "整文件"
        head = fh.read(limit)
        if not _is_all_ascii(head):
            return head, "文件开头"
        for offset, where in ((size // 2, "文件中段"), (max(0, size - limit // 2), "文件末尾")):
            block = _probe_clean_block(fh, offset, limit // 4)
            if block and not _is_all_ascii(block):
                return block, where
        return head, "文件开头"


def detect_file(path: os.PathLike | str) -> DetectResult:
    """检测文件编码。大文件只取几处样本（开头/中段/末段），不会整读。"""
    sample, where = sample_file_bytes(path)
    result = detect_bytes(sample)
    if where != "整文件":
        result.reason = f"{result.reason}（取样自{where}）".strip()
    return result


# 解码 / 编码

def decode_bytes(
    data: bytes,
    encoding: str = "auto",
    errors: str = "strict",
) -> tuple[str, str, Optional[DetectResult]]:
    """把字节解码成文本。encoding='auto' 时自动检测。返回 (文本, 实际编码, 检测结果)。"""
    detected: Optional[DetectResult] = None
    codec_name = encoding
    if not encoding or str(encoding).lower() == "auto":
        detected = detect_bytes(data)
        codec_name = detected.encoding
    codec = canonical_codec(codec_name)
    text = data.decode(codec, errors)
    return strip_leading_bom(text), codec, detected


def resolve_output_codec(
    encoding: str,
    bom_mode: str = "keep",
    source_had_bom: bool = False,
) -> tuple[str, bool]:
    """
    解析目标编码与写 BOM 策略。
    bom_mode: 'default' 按编码惯例 / 'add' 强制加 / 'none' 不加 / 'keep' 与源文件一致
    """
    codec = canonical_codec(encoding)
    default_bom = False
    if codec == "utf-8-sig":
        codec, default_bom = "utf-8", True
    elif codec == "utf-16":
        codec, default_bom = "utf-16-le", True
    elif codec == "utf-32":
        codec, default_bom = "utf-32-le", True

    if bom_mode == "add":
        want_bom = True
    elif bom_mode == "none":
        want_bom = False
    elif bom_mode == "keep":
        want_bom = bool(source_had_bom)
    else:
        want_bom = default_bom
    if codec not in BOM_BYTES:
        want_bom = False
    return codec, want_bom


def encode_text(
    text: str,
    encoding: str,
    bom_mode: str = "keep",
    source_had_bom: bool = False,
    errors: str = "strict",
) -> tuple[bytes, str]:
    """把文本编码成字节，并按策略决定是否写 BOM。返回 (字节, 实际编码)。"""
    codec, want_bom = resolve_output_codec(encoding, bom_mode, source_had_bom)
    data = text.encode(codec, errors)
    if want_bom:
        bom = BOM_BYTES.get(codec)
        if bom and not data.startswith(bom):
            data = bom + data
    return data, codec


NEWLINE_MODES = {
    "keep": "保持原样",
    "crlf": "统一为 CRLF (Windows)",
    "lf": "统一为 LF (Unix/macOS)",
    "cr": "统一为 CR (旧版 Mac)",
}

BOM_MODES = {
    "keep": "与原文件一致",
    "none": "不添加 BOM",
    "add": "添加 BOM",
    "default": "按编码惯例",
}

OUTPUT_MODES = {
    "newdir": "输出到新目录（推荐）",
    "suffix": "原目录加后缀另存",
    "inplace": "原地覆盖原文件",
}


def convert_newlines(text: str, mode: str = "keep") -> str:
    """按需统一换行符，'keep' 时原样返回。"""
    if mode == "keep" or not mode:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "lf":
        return normalized
    if mode == "crlf":
        return normalized.replace("\n", "\r\n")
    if mode == "cr":
        return normalized.replace("\n", "\r")
    return text


# 批量转码

@dataclass
class ConvertOptions:
    """一次批量转换的全部参数。"""

    source_encoding: str = "auto"
    target_encoding: str = "utf-8"
    bom_mode: str = "keep"
    newline: str = "keep"
    on_decode_error: str = "skip"
    on_encode_error: str = "skip"
    skip_if_same: bool = True
    copy_unchanged: bool = True
    output_mode: str = "newdir"
    output_dir: Optional[Path] = None
    suffix: str = "_converted"
    backup: bool = True
    keep_mtime: bool = False
    dry_run: bool = False

    def copy(self, **changes) -> "ConvertOptions":
        return _replace(self, **changes)


@dataclass
class FileResult:
    """单个文件的处理结果。"""

    src: Path
    dst: Optional[Path] = None
    status: str = "pending"
    source_encoding: str = ""
    source_label: str = ""
    target_encoding: str = ""
    confidence: float = 0.0
    reason: str = ""
    error: str = ""
    size_before: int = 0
    size_after: int = 0
    had_bom: bool = False
    created_bom: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("converted", "copied", "unchanged", "preview")

    def status_text(self) -> str:
        return {
            "converted": "已转换",
            "copied": "已复制（无需转换）",
            "unchanged": "跳过（已是目标编码）",
            "skipped": "已跳过",
            "failed": "失败",
            "preview": "待转换",
            "pending": "待处理",
        }.get(self.status, self.status)

    def summary(self) -> str:
        base = f"{self.status_text()}：{self.src.name}"
        if self.error:
            return f"{base} —— {self.error}"
        detail = []
        if self.source_encoding:
            detail.append(
                f"{self.source_label or self.source_encoding} → {encoding_label(self.target_encoding)}"
            )
        if self.size_before:
            detail.append(f"{format_size(self.size_before)} → {format_size(self.size_after)}")
        if self.reason:
            detail.append(self.reason)
        return f"{base} —— {'，'.join(detail)}" if detail else base


def format_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / 1024 / 1024:.2f} MB"
    return f"{num / 1024 / 1024 / 1024:.2f} GB"


def parse_exts(text: Optional[str | Iterable[str]]) -> set[str]:
    """把 'txt, md; .csv' 之类的字符串解析成 {'.txt', '.md', '.csv'}。空表示全部。"""
    if text is None:
        return set(DEFAULT_EXTS)
    if isinstance(text, str):
        raw = list(text.replace(";", ",").replace(" ", ",").split(","))
    else:
        raw = list(text)
    exts: set[str] = set()
    for piece in raw:
        piece = piece.strip().lower()
        if not piece:
            continue
        if piece in ("*", "*.*", "all", "全部"):
            return set()
        if not piece.startswith("."):
            piece = "." + piece
        exts.add(piece)
    return exts


def iter_files(
    inputs: Sequence[Path | str],
    exts: Optional[set[str]] = None,
    recursive: bool = True,
    exclude_dirs: Sequence[Path | str] = (),
    skip_hidden: bool = True,
) -> Iterator[tuple[Path, Path]]:
    """
    遍历输入里的所有目标文件。产出 (文件路径, 所属根目录)。
    传入单个文件时，根目录取其父目录。
    """
    exclude = set()
    for item in exclude_dirs:
        try:
            exclude.add(Path(item).resolve())
        except OSError:
            continue

    seen: set[Path] = set()
    for item in inputs:
        root = Path(item).expanduser()
        if root.is_dir():
            base = root
            iterator = base.rglob("*") if recursive else base.glob("*")
            for path in iterator:
                if not path.is_file():
                    continue
                if skip_hidden and path.name.startswith("."):
                    continue
                if path.name.endswith(".bak") or path.name.startswith("~$"):
                    continue
                if _under_any(path, exclude):
                    continue
                if exts and path.suffix.lower() not in exts:
                    continue
                resolved = _try_resolve(path)
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield path, base
        elif root.is_file():
            if exts and root.suffix.lower() not in exts:
                continue
            resolved = _try_resolve(root)
            if resolved in seen:
                continue
            seen.add(resolved)
            yield root, root.parent


def _try_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _under_any(path: Path, dirs: set[Path]) -> bool:
    if not dirs:
        return False
    resolved = _try_resolve(path)
    for directory in dirs:
        if resolved == directory or directory in resolved.parents:
            return True
    return False


def target_tag(encoding: str, bom_mode: str = "none", source_had_bom: bool = False) -> str:
    """生成输出目录/后缀用的短标签，例如 utf8、utf8bom、gbk。"""
    codec, want_bom = resolve_output_codec(encoding, bom_mode, source_had_bom)
    base = {
        "utf-8": "utf8",
        "utf-16-le": "utf16le",
        "utf-16-be": "utf16be",
        "utf-32-le": "utf32le",
        "utf-32-be": "utf32be",
        "gbk": "gbk",
        "gb2312": "gb2312",
        "gb18030": "gb18030",
        "big5": "big5",
        "big5hkscs": "big5hkscs",
        "shift_jis": "sjis",
        "euc_kr": "euckr",
        "cp1252": "ansi",
        "iso8859-1": "latin1",
        "ascii": "ascii",
    }.get(codec, codec.replace("-", ""))
    if want_bom and codec == "utf-8":
        base += "bom"
    return base


def build_destination(
    src: Path,
    root: Path,
    options: ConvertOptions,
    tag: Optional[str] = None,
) -> Path:
    """按输出模式算出目标路径。"""
    if options.output_mode == "inplace":
        return src

    if tag is None:
        tag = target_tag(options.target_encoding, options.bom_mode)

    if options.output_mode == "suffix":
        return src.with_name(f"{src.stem}{options.suffix or '_converted'}{src.suffix}")

    out_root = options.output_dir
    if out_root is None:
        out_root = root.parent / f"{root.name}_{tag}" if root.is_dir() else src.parent / f"{tag}_output"
    else:
        out_root = Path(out_root)

    try:
        rel = src.resolve().relative_to(root.resolve()) if root.is_dir() else Path(src.name)
    except (ValueError, OSError):
        rel = Path(src.name)
    return Path(out_root) / rel


def _remove_quietly(path: Path) -> None:
    """删掉临时文件，失败也不吭声（不能因为清理失败掩盖真正的问题）。"""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


class _OutputSink:
    """
    转码结果的去处：可以真的写文件，也可以什么都不写（预演模式），
    还能一边写一边和原文件逐字节比对，用来判断“转完其实和原文件一模一样”。

    比对是用同一个数据流做的，所以不需要额外的读盘遍数，内存也始终只有一块。
    """

    def __init__(self, fout=None, compare_to: Optional[Path] = None) -> None:
        self._fout = fout
        self._source = open(compare_to, "rb") if compare_to is not None else None
        self._buffer = b""
        self._buffer_pos = 0
        self.size = 0
        self.identical = compare_to is not None

    def write(self, data: bytes) -> None:
        if not data:
            return
        self.size += len(data)
        if self._fout is not None:
            self._fout.write(data)
        if self._source is None or not self.identical:
            return
        position = 0
        while position < len(data):
            if self._buffer_pos >= len(self._buffer):
                self._buffer = self._source.read(CHUNK_SIZE)
                self._buffer_pos = 0
                if not self._buffer:
                    self.identical = False        # 结果比原文件长
                    return
            take = min(len(self._buffer) - self._buffer_pos, len(data) - position)
            if self._buffer[self._buffer_pos:self._buffer_pos + take] != data[position:position + take]:
                self.identical = False
                return
            self._buffer_pos += take
            position += take

    def finish(self) -> bool:
        """收尾：确认原文件没有多出来的尾巴，返回“是否和原文件完全一致”。"""
        if self._source is not None:
            if self.identical and (len(self._buffer) - self._buffer_pos > 0
                                   or self._source.read(1)):
                self.identical = False
            self.close()
        return self.identical

    def close(self) -> None:
        """放开用于比对的源文件句柄（出错路径也别忘了调）。"""
        if self._source is not None:
            try:
                self._source.close()
            except OSError:
                pass
            self._source = None


def _stream_convert(
    src: Path,
    sink: _OutputSink,
    source_codec: str,
    target_codec: str,
    want_bom: bool = False,
    newline: str = "keep",
    decode_errors: str = "strict",
    encode_errors: str = "strict",
) -> None:
    """
    分块转码：读一块 → 解码 → 处理换行 → 编码 → 交给 sink。

    内存里任何时刻只有一个块（默认 1 MB）的字节和它对应的文本，
    所以 500 MB 的日志和 5 KB 的小文件占用一样多。
    用增量解码器/编码器保证被切在块边界上的多字节字符能正确拼回来。
    """
    decoder = codecs.getincrementaldecoder(source_codec)(errors=decode_errors)
    encoder = codecs.getincrementalencoder(target_codec)(errors=encode_errors)

    if want_bom:
        bom = BOM_BYTES.get(target_codec)
        if bom:
            sink.write(bom)

    pending_cr = False
    first = True
    offset = 0
    with src.open("rb") as fin:
        while True:
            chunk = fin.read(CHUNK_SIZE)
            final = not chunk
            try:
                text = decoder.decode(chunk, final)
            except UnicodeDecodeError as exc:
                raise DecodeFailure(offset + getattr(exc, "start", 0), exc) from exc
            if first:
                text = strip_leading_bom(text)
                first = False
            if newline != "keep":
                if pending_cr:
                    text = "\r" + text
                    pending_cr = False
                if not final and text.endswith("\r"):
                    # CRLF 恰好被切在块边界上，先留一个字符到下一块一起处理
                    pending_cr = True
                    text = text[:-1]
                text = convert_newlines(text, newline)
            if text or final:
                sink.write(encoder.encode(text, final))
            if final:
                return
            offset += len(chunk)


def _starts_with_bom(data: bytes) -> bool:
    return any(data.startswith(bom) for bom, _, _ in BOM_ENTRIES)


def convert_bytes(
    data: bytes,
    options: ConvertOptions,
) -> tuple[bytes, DetectResult, str, bool]:
    """
    转换一段字节。返回 (新字节, 检测结果, 实际源编码, 源文件是否有 BOM)。
    解码/编码失败会抛出 UnicodeDecodeError / UnicodeEncodeError，由调用方处理。
    """
    decode_errors = "replace" if options.on_decode_error == "replace" else "strict"
    encode_errors = "replace" if options.on_encode_error == "replace" else "strict"
    text, source_codec, detected = decode_bytes(
        data, options.source_encoding, errors=decode_errors,
    )
    had_bom = bool(detected.has_bom) if detected else _starts_with_bom(data)
    if detected is None:
        detected = DetectResult(source_codec, 1.0, "按指定编码读取", has_bom=had_bom)

    text = convert_newlines(text, options.newline)
    new_data, _ = encode_text(
        text,
        options.target_encoding,
        bom_mode=options.bom_mode,
        source_had_bom=had_bom,
        errors=encode_errors,
    )
    return new_data, detected, source_codec, had_bom


def convert_file(src: Path, options: ConvertOptions, root: Optional[Path] = None) -> FileResult:
    """
    转换单个文件。不会抛出异常，所有问题都体现在返回值里。

    读、转、写都按 CHUNK_SIZE 分块进行，内存占用与文件大小无关；
    大文件的编码检测也只取开头/中段/末段几处样本，不会把整个文件读进内存。
    """
    src = Path(src)
    root = Path(root) if root is not None else src.parent
    result = FileResult(src=src, target_encoding=options.target_encoding)

    try:
        size = src.stat().st_size
        sample, sample_where = sample_file_bytes(src)
    except OSError as exc:
        result.status = "failed"
        result.error = f"读取失败：{exc}"
        return result

    result.size_before = size

    if looks_binary(sample):
        result.status = "skipped"
        result.error = "疑似二进制文件，未处理"
        return result

    detected = detect_bytes(sample)
    if sample_where != "整文件":
        detected.reason = f"{detected.reason}（取样自{sample_where}）".strip()

    # 源编码：用户指定优先，否则用检测结果
    source_specified = bool(options.source_encoding) and str(options.source_encoding).lower() != "auto"
    try:
        if source_specified:
            source_codec = canonical_codec(str(options.source_encoding))
            had_bom = _starts_with_bom(sample)
        else:
            source_codec = canonical_codec(detected.encoding)
            had_bom = detected.has_bom
        target_codec, want_bom = resolve_output_codec(
            options.target_encoding, options.bom_mode, had_bom)
    except EncodingNotSupported as exc:
        result.status = "failed"
        result.error = str(exc)
        return result

    result.source_encoding = source_codec
    result.source_label = encoding_label(source_codec)
    result.had_bom = had_bom
    if source_specified:
        result.confidence = 1.0
        result.reason = f"按指定的 {encoding_label(source_codec)} 读取"
    else:
        result.confidence = detected.confidence
        result.reason = detected.reason

    try:
        dst = build_destination(src, root, options)
    except OSError as exc:
        result.status = "failed"
        result.error = f"无法确定输出路径：{exc}"
        return result
    result.dst = dst

    # 转出来的字节有没有可能和原文件一模一样？只有“可能一样”才值得顺手逐字节比对。
    same_group = codec_may_keep_bytes(source_codec, target_codec)
    ascii_to_ascii_safe = detected.is_ascii_only and target_codec in _ASCII_SAFE_CODECS
    compare_worthwhile = same_group or ascii_to_ascii_safe
    # 不用读文件就能确定“字节不会变”的判据要更严格：
    #   * 纯 ASCII 的情况必须确认整个文件都取样过了；
    #   * 手动指定了源编码时不做这个捷径 —— 那时用户要的是“按我说的编码读完整个文件”，
    #     跳过去就等于跳过了校验，会把“编码写错但碰巧不用改字节”的文件静默放过。
    quick_skip = (same_group or (ascii_to_ascii_safe and sample_where == "整文件")) \
        and not source_specified

    # 便宜的一步：编码没变、BOM 策略没变、也不动换行 → 字节不可能变，整文件都不用读
    if (options.skip_if_same and options.newline == "keep"
            and want_bom == had_bom and quick_skip):
        return _finish_unchanged(result, options, dst)

    # 选项里的 skip 表示“整个文件跳过”，对 Python 来说就是 strict（抛错让人接管）
    decode_errors = "replace" if options.on_decode_error == "replace" else "strict"
    encode_errors = "replace" if options.on_encode_error == "replace" else "strict"

    tmp = dst.with_name(f"{dst.name}.tmp{os.getpid()}")
    handle = None
    sink = _OutputSink(None, src if compare_worthwhile else None)
    try:
        if not options.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            handle = tmp.open("wb")
            sink = _OutputSink(handle, src if compare_worthwhile else None)
        _stream_convert(
            src, sink, source_codec, target_codec, want_bom,
            options.newline, decode_errors, encode_errors,
        )
        if handle is not None:
            handle.flush()
            os.fsync(handle.fileno())
        identical = sink.finish() and sink.size == size
    except DecodeFailure as exc:
        result.status = "failed"
        result.error = (
            f"{encoding_label(source_codec)} 解码失败（文件第 {exc.offset} 字节附近），"
            "请确认源编码，或把“解码失败时”改为用替代字符继续"
        )
    except UnicodeDecodeError as exc:
        result.status = "failed"
        result.error = f"{encoding_label(source_codec)} 解码失败：{exc}"
    except UnicodeEncodeError as exc:
        bad = exc.object[exc.start:exc.start + 1]
        result.status = "failed"
        result.error = (
            f"目标编码 {encoding_label(options.target_encoding)} 无法表示字符 “{bad}”，"
            "建议目标编码改用 GBK / GB18030 / UTF-8"
        )
    except OSError as exc:
        result.status = "failed"
        result.error = f"读写失败：{exc}"
    else:
        result.size_after = sink.size
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        sink.close()

    if result.status == "failed":
        _remove_quietly(tmp)
        return result

    if identical and options.skip_if_same:
        _remove_quietly(tmp)
        return _finish_unchanged(result, options, dst)

    if options.dry_run:
        result.status = "preview"
        return result

    if options.output_mode == "inplace" and options.backup:
        backup_path = src.with_name(src.name + ".bak")
        if not backup_path.exists():
            try:
                shutil.copy2(src, backup_path)
            except OSError as exc:
                _remove_quietly(tmp)
                result.status = "failed"
                result.error = f"备份失败，已中止（原文件未改动）：{exc}"
                return result

    try:
        stat = src.stat()
        os.replace(tmp, dst)
        if options.keep_mtime and options.output_mode == "inplace":
            os.utime(dst, (stat.st_atime, stat.st_mtime))
    except OSError as exc:
        _remove_quietly(tmp)
        result.status = "failed"
        result.error = f"写入失败：{exc}"
        return result

    result.status = "converted"
    return result


def _finish_unchanged(result: FileResult, options: ConvertOptions, dst: Path) -> FileResult:
    """
    文件本来就是目标编码（或转完必然一模一样）。
    输出到新目录时仍会原样复制一份过去，好让输出目录是完整的；
    原地覆盖 / 加后缀模式则直接跳过。
    """
    result.size_after = result.size_before
    if options.output_mode == "newdir" and options.copy_unchanged:
        result.status = "copied"
        if not options.dry_run:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result.src, dst)
            except OSError as exc:
                result.status = "failed"
                result.error = f"复制失败：{exc}"
    else:
        result.status = "unchanged"
    return result


def convert_many(
    inputs: Sequence[Path | str],
    options: ConvertOptions,
    exts: Optional[set[str]] = None,
    recursive: bool = True,
    exclude_dirs: Sequence[Path | str] = (),
    on_file=None,
    should_stop=None,
) -> list[FileResult]:
    """批量转换入口（单线程）。on_file(result) 可用于实时回调。"""
    results: list[FileResult] = []
    for src, root in iter_files(inputs, exts, recursive, exclude_dirs):
        if should_stop is not None and should_stop():
            break
        result = convert_file(src, options, root)
        results.append(result)
        if on_file is not None:
            on_file(result)
    return results


def count_status(results: Sequence[FileResult]) -> dict[str, int]:
    """统计各状态文件数量。"""
    stats: dict[str, int] = {}
    for item in results:
        stats[item.status] = stats.get(item.status, 0) + 1
    return stats


# 预览 / 详情

@dataclass
class FileInfo:
    path: Path
    size: int
    encoding: str
    label: str
    confidence: float
    reason: str
    has_bom: bool
    is_ascii_only: bool
    is_binary: bool
    preview: str

    def describe(self) -> str:
        return (
            f"{self.path.name}：{self.label}（置信度 {self.confidence * 100:.0f}%）"
            f" · {self.reason} · {format_size(self.size)}"
        )


def inspect_file(
    path: Path | str,
    encoding: str = "auto",
    max_chars: int = 2000,
    errors: str = "replace",
) -> FileInfo:
    """
    读取文件，返回编码检测结论和前若干字符的预览文本。

    只读开头一小段（大文件也一样），不会把整个文件读进内存。
    """
    p = Path(path)
    size = p.stat().st_size
    sample, sample_where = sample_file_bytes(p)
    detected = detect_bytes(sample)
    if sample_where != "整文件":
        detected.reason = f"{detected.reason}（取样自{sample_where}）".strip()
    is_binary = looks_binary(sample)

    with p.open("rb") as fh:
        head = fh.read(PREVIEW_SAMPLE_BYTES)

    codec = encoding
    if not encoding or str(encoding).lower() == "auto":
        codec = detected.encoding
    try:
        text, codec_used, _ = decode_bytes(head, codec, errors=errors)
        preview = text[:max_chars]
    except (UnicodeDecodeError, EncodingNotSupported):
        preview = f"[无法用 {encoding_label(codec)} 解码此文件]"
        codec_used = codec
    except UnicodeError:
        preview = "[解码异常]"
        codec_used = codec

    return FileInfo(
        path=p,
        size=size,
        encoding=codec_used,
        label=encoding_label(codec_used),
        confidence=detected.confidence,
        reason=detected.reason,
        has_bom=detected.has_bom,
        is_ascii_only=detected.is_ascii_only,
        is_binary=is_binary,
        preview=preview,
    )


if __name__ == "__main__":  # 自检：python txt_encoder_core.py <文件或目录> ...
    import sys

    targets = sys.argv[1:]
    if not targets:
        print("用法: python txt_encoder_core.py <文件或目录> ...")
        raise SystemExit(1)
    for item in targets:
        for file_path, _root in iter_files([item]):
            print(inspect_file(file_path).describe())
