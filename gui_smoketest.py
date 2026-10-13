#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_smoketest.py —— 图形界面自动冒烟测试（不需要人工点击）。

它会真的创建窗口、执行一次“扫描预览”和一次“开始转换”，
然后检查表格里的结果是否符合预期，最后关闭窗口。

用法：python gui_smoketest.py [测试目录]
如果不给目录，会先在 “示例文件” 里生成样例（若不存在）。
"""

from __future__ import annotations

import sys
import shutil
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from txt_encoder_core import make_console_tolerant  # noqa: E402
from txt_encoder_gui import EncoderApp  # noqa: E402


def pump(app: EncoderApp, seconds: float = 20.0, until=None) -> bool:
    """驱动 Tk 事件循环，直到条件满足或超时。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return False


def idle(app: EncoderApp) -> bool:
    return app._worker is None or not app._worker.is_alive()


def main() -> int:
    make_console_tolerant()
    demo = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "示例文件"
    if not demo.exists():
        import make_sample_files
        sys.argv = [sys.argv[0], str(demo)]
        make_sample_files.main()

    failures: list[str] = []

    def check(cond: bool, title: str, detail: str = "") -> None:
        print(f"  [{'通过' if cond else '失败'}] {title}  {'' if cond else detail}")
        if not cond:
            failures.append(title)

    print(f"启动图形界面，测试目录：{demo}")
    app = EncoderApp()
    app.update()
    check(app.winfo_exists() == 1, "窗口创建成功")

    app._add_input(str(demo))
    check(";" in app.input_var.get() or app.input_var.get() == str(demo), "输入框写入路径")

    # ---- 扫描
    app._start_scan()
    ok = pump(app, 60, lambda: idle(app) and not app.progress["value"] == 0)
    app.update()
    rows = app.tree.get_children()
    check(ok, "扫描任务结束")
    check(len(rows) > 0, f"表格有数据（{len(rows)} 行）")
    statuses = [app.tree.set(row, "status") for row in rows]
    check(any(s == "待转换" for s in statuses), "扫描结果里有“待转换”的文件", str(statuses))
    check(any(s in ("跳过（已是目标编码）", "已复制（无需转换）") for s in statuses),
          "已经符合目标编码的文件被识别出来（不重复转换）", str(statuses))
    sources = {app.tree.set(row, "src") for row in rows}
    check(any("UTF" in s or "GB" in s or "Big5" in s for s in sources), "检测编码列有内容", str(sources))

    # ---- 预览
    app.tree.selection_set(rows[0])
    app.update()
    app._refresh_preview()
    preview = app.preview_text.get("1.0", "end").strip()
    check(len(preview) > 0 and "无法" not in preview, "选中文件后能显示内容预览",
          preview[:60])

    # ---- 转换（输出到新目录）
    app.mode_var.set("newdir")
    app.out_dir_var.set(str(demo.parent / "_gui_smoke_out"))
    app.tgt_var.set("UTF-8（推荐，跨平台通用）")
    app._start_convert()
    ok = pump(app, 120, lambda: idle(app) and app.progress["value"] >= app.progress["maximum"])
    app.update()
    check(ok, "转换任务结束")
    final = [app.tree.set(row, "status") for row in rows]
    check(any(s == "已转换" for s in final), "有文件被转换", str(final))
    check(not any(s == "失败" for s in final), "没有失败项", str(final))
    out_dir = demo.parent / "_gui_smoke_out"
    check(out_dir.exists(), "输出目录已生成", str(out_dir))

    # ---- 转换后的文件必须能被 UTF-8 正确解码
    bad = []
    for path in out_dir.rglob("*"):
        if path.is_file():
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                bad.append(f"{path.name}: {exc}")
    check(not bad, "输出文件全部是合法 UTF-8", "; ".join(bad))

    # ---- 原地覆盖 + 备份
    probe = demo.parent / "_gui_smoke_probe"
    probe.mkdir(exist_ok=True)
    target_file = probe / "probe.txt"
    target_file.write_bytes("原地覆盖冒烟测试。\n".encode("gbk"))
    app.input_var.set(str(probe))
    app.mode_var.set("inplace")
    app.backup_var.set(True)
    app._pairs = []
    app._start_convert()
    ok = pump(app, 60, lambda: idle(app) and app.progress["value"] >= app.progress["maximum"])
    check(ok and target_file.read_bytes() == "原地覆盖冒烟测试。\n".encode("utf-8"),
          "图形界面里原地覆盖也生效")
    check(target_file.with_name("probe.txt.bak").exists(), "原地覆盖生成了备份")

    app.destroy()

    # 清理本次冒烟测试产生的目录（只删自己建的这两个）
    for junk in (demo.parent / "_gui_smoke_out", demo.parent / "_gui_smoke_probe"):
        if junk.parent == demo.parent and junk.name.startswith("_gui_smoke"):
            shutil.rmtree(junk, ignore_errors=True)

    print()
    if failures:
        print(f"有 {len(failures)} 项未通过：{failures}")
        return 1
    print("图形界面冒烟测试全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
