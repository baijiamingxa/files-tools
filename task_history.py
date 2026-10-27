#!/usr/bin/env python3
"""本机任务记录。只存路径和结果，不序列化选项、密码或文件内容。"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

STATES = ("进行中", "已完成", "部分完成", "失败", "已取消", "意外中断")


def clean_text(value):
    text = str(value or "")
    def redact(match):
        try:
            url = urlsplit(match.group())
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[链接已隐藏]"
    return re.sub(r"https?://[^\s<>\"']+", redact, text)[:8000]


def item(source="", output="", state="已完成", detail=""):
    return {"input": clean_text(source), "output": clean_text(output),
            "state": clean_text(state), "detail": clean_text(detail)}


class HistoryStore:
    def __init__(self, path, recover=False, keep=2000):
        self.path, self.keep = Path(path), keep
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, kind TEXT NOT NULL, started REAL NOT NULL, ended REAL, status TEXT NOT NULL, summary TEXT NOT NULL, items TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started DESC)")
            if recover:
                db.execute("UPDATE tasks SET status='意外中断', ended=?, summary='上次程序未正常结束；请核对已生成的结果，记录不会自动重试任务。' WHERE status='进行中'", (time.time(),))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=3)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin(self, kind, inputs=()):
        token = uuid.uuid4().hex
        values = [item(path, state="等待处理") for path in list(inputs)[:5000]]
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)", (token, clean_text(kind), time.time(), None, "进行中", "正在处理…", json.dumps(values, ensure_ascii=False)))
        return token

    def finish(self, token, items=(), error="", cancelled=False, summary=""):
        original = list(items)
        values = [item(row.get("input", ""), row.get("output", ""), row.get("state", ""), row.get("detail", "")) for row in original[:5000]]
        failed = sum(row.get("state") in ("失败", "部分完成", "无法转换", "不一致", "未处理") for row in original)
        succeeded = sum(row.get("state") in ("已完成", "完成", "部分完成", "一致", "已跳过", "保持原样") for row in original)
        status = "已取消" if cancelled else "部分完成" if (error or failed) and succeeded else "失败" if error or failed else "已完成"
        note = summary or f"成功 / 跳过 {succeeded} 项，失败 {failed} 项，共 {len(original)} 项。"
        if len(original) > len(values):
            note += " 明细仅保存前 5000 项。"
        if error:
            note += "\n" + clean_text(error)
        with self.lock, self.connect() as db:
            db.execute("UPDATE tasks SET ended=?,status=?,summary=?,items=? WHERE id=? AND status='进行中'", (time.time(), status, clean_text(note), json.dumps(values, ensure_ascii=False), token))
            db.execute("DELETE FROM tasks WHERE id IN (SELECT id FROM tasks WHERE status!='进行中' ORDER BY started DESC LIMIT -1 OFFSET ?)", (self.keep,))

    def query(self, text="", status="全部", limit=500, include_items=True):
        terms, args = [], []
        if text:
            terms.append("(kind LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR items LIKE ? ESCAPE '\\')")
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [pattern] * 3
        if status != "全部":
            terms.append("status=?")
            args.append(status)
        columns = "id,kind,started,ended,status,summary," + ("items" if include_items else "'[]'")
        sql = "SELECT " + columns + " FROM tasks" + (" WHERE " + " AND ".join(terms) if terms else "") + " ORDER BY started DESC LIMIT ?"
        with self.lock, self.connect() as db:
            records = db.execute(sql, (*args, limit)).fetchall()
        return [dict(zip(("id", "kind", "started", "ended", "status", "summary", "items"), (*row[:6], json.loads(row[6])))) for row in records]

    def get(self, token):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT id,kind,started,ended,status,summary,items FROM tasks WHERE id=?", (token,)).fetchone()
        return dict(zip(("id", "kind", "started", "ended", "status", "summary", "items"), (*row[:6], json.loads(row[6])))) if row else None

    def delete(self, ids=None):
        with self.lock, self.connect() as db:
            if ids is None:
                db.execute("DELETE FROM tasks WHERE status!='进行中'")
            else:
                db.executemany("DELETE FROM tasks WHERE id=? AND status!='进行中'", ((value,) for value in ids))

    def export(self, path, text="", status="全部"):
        def cell(value):
            value = str(value or "")
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        records = self.query(text, status, self.keep + 1000)
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(("任务", "开始时间", "结束时间", "状态", "摘要", "源文件 / 链接", "输出", "明细状态", "说明"))
            for record in records:
                stamp = lambda value: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if value else ""
                for row in record["items"] or [item()]:
                    writer.writerow([cell(value) for value in (record["kind"], stamp(record["started"]), stamp(record["ended"]), record["status"], record["summary"], row["input"], row["output"], row["state"], row["detail"])])
        return len(records)


class HistoryRecorder:
    """记录失败不影响文件任务；错误留给记录窗口显示。"""
    def __init__(self, path):
        self.error, self.store = "", None
        try:
            self.store = HistoryStore(path, recover=True)
        except Exception as exc:
            self.error = str(exc)

    def begin(self, kind, inputs=()):
        try:
            return self.store.begin(kind, inputs) if self.store else None
        except Exception as exc:
            self.error = str(exc)

    def finish(self, token, **kwargs):
        if token and self.store:
            try:
                self.store.finish(token, **kwargs)
            except Exception as exc:
                self.error = str(exc)


def tracked(recorder, kind, work, inputs=(), describe=None, cancelled=None):
    """在工作线程内记录，GUI 不执行文件或数据库批量写入。"""
    if recorder is None:
        return work
    def run(*args, **kwargs):
        token = recorder.begin(kind, inputs)
        result, error = None, ""
        try:
            result = work(*args, **kwargs)
            return result
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            try:
                rows = describe(result) if describe else [item(path, state="失败" if error else "已完成", detail=error) for path in inputs]
                if not rows and error:
                    rows = [item(path, state="已取消" if cancelled and cancelled() else "失败", detail=error) for path in inputs]
                recorder.finish(token, items=rows, error=error, cancelled=bool(cancelled and cancelled()))
            except Exception as exc:
                recorder.finish(token, error=error or ("结果明细未能记录：" + str(exc)))
    return run


def file_rows(rows):
    result = []
    for row in rows or []:
        source = getattr(row, "path", getattr(row, "src", ""))
        error = getattr(row, "error", "")
        state = str(getattr(row, "status", "已完成"))
        state = {"converted": "已完成", "copied": "已完成", "unchanged": "已跳过", "skipped": "已跳过", "failed": "失败", "preview": "已完成", "已计算": "已完成", "已替换": "已完成"}.get(state, state)
        if state in ("待转换", "转换中", "待替换", "待处理", "处理中"):
            state = "未处理"
        if error:
            state = "失败"
        output = getattr(row, "destination", getattr(row, "dst", "")) if state in ("已完成", "一致") else ""
        detail = error or getattr(row, "detail", "") or getattr(row, "reason", "")
        if getattr(row, "backup", ""):
            detail += "\n备份：" + str(row.backup)
        if getattr(row, "digests", None):
            detail += "\n" + " · ".join(f"{name.upper()}: {value}" for name, value in row.digests.items())
        result.append(item(source, output or "", state, detail))
    return result
