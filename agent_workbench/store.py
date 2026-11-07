from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import uuid
from .model_config import public_model


ACTIVE = {"queued", "planning", "executing", "reviewing", "repairing", "patching"}
MODES = {"combined", "codex", "harness"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "workbench.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events ON events(task_id,seq);
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=20)
        db.execute("PRAGMA busy_timeout=20000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, prompt, cwd, mode="combined", timeout=600, checks=None, permission="normal", auto_checks=False,
               attachments=None):
        if not isinstance(cwd, (str, Path)) or not str(cwd).strip():
            raise ValueError("请填写项目文件夹")
        path = Path(cwd).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("工作目录必须是现有文件夹")
        if mode not in MODES:
            raise ValueError("未知运行模式")
        if permission not in {"normal", "full"}:
            raise ValueError("未知权限设置")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise ValueError("任务描述不能为空，且最多 32000 字符")
        timeout = float(timeout)
        if not 10 <= timeout <= 7200:
            raise ValueError("单轮超时范围为 10–7200 秒")
        checks = checks or []
        if not isinstance(checks, list) or len(checks) > 10:
            raise ValueError("最多配置 10 条验收命令")
        for argv in checks:
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v and "\0" not in v for v in argv):
                raise ValueError('验收命令使用参数数组，例如 [["python", "-m", "unittest"]]')
        task = dict(id=uuid.uuid4().hex[:12], prompt=prompt.strip(), cwd=str(path), mode=mode,
                    timeout=timeout, checks=checks, state="queued", stage="plan" if mode == "combined" else "execute",
                    repair_count=0, runtime_failures=0, sessions={}, plan=None, reviews=[],
                    runs=[], artifacts=[], created=now(), updated=now(), stop_reason=None,
                    cancel_requested=False, takeover=False, baseline=None, last_result=None,
                    harness_model=None, kind="work", permission=permission, escalate=False,
                    auto_checks=bool(auto_checks), attachments=attachments or [])
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(task, ensure_ascii=False)))
        self.event(task["id"], "created", {"mode": mode, "cwd": str(path), "permission": permission})
        return task

    def create_study(self, text, executor, cwd, attachments=None):
        task = dict(id=uuid.uuid4().hex[:12], kind="study", prompt=text, cwd=str(cwd), mode="study", executor=executor,
                    timeout=900, checks=[], state="queued", stage="chat", repair_count=0, runtime_failures=0,
                    sessions={}, seen={}, messages=[dict(role="user", text=text, at=now(), attachments=attachments or [])], plan=None, reviews=[],
                    runs=[], artifacts=[], created=now(), updated=now(), stop_reason=None, cancel_requested=False,
                    takeover=False, baseline=None, last_result=None, harness_model=None)
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], json.dumps(task, ensure_ascii=False)))
        self.event(task["id"], "created", {"mode": "study", "cwd": str(cwd), "executor": executor})
        return task

    def delete(self, task_id):
        with self.connect() as db:
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            db.execute("DELETE FROM events WHERE task_id=?", (task_id,))

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        return json.loads(row[0])

    def list(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM tasks ORDER BY rowid DESC")]

    def update(self, task_id, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = json.loads(row[0])
            task.update(changes, updated=now())
            db.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(task, ensure_ascii=False), task_id))
        return task

    def event(self, task_id, kind, data):
        with self.connect() as db:
            db.execute("INSERT INTO events(task_id,at,kind,data) VALUES (?,?,?,?)",
                       (task_id, now(), kind, json.dumps(data, ensure_ascii=False)))

    def events(self, task_id, after=0, limit=500):
        with self.connect() as db:
            rows = db.execute("SELECT seq,at,kind,data FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (task_id, after, limit)).fetchall()
        return [dict(seq=r[0], at=r[1], kind=r[2], data=json.loads(r[3])) for r in rows]

    def recover(self):
        for task in self.list():
            if task["state"] in ACTIVE and task["state"] != "queued":
                self.update(task["id"], state="interrupted", stop_reason="工作台进程中断；检查产物后显式恢复，可使用已保存会话。")
                self.event(task["id"], "interrupted", {"stage": task["stage"]})

    def summary(self, task_id):
        task = self.get(task_id)
        fields = ("id", "prompt", "cwd", "mode", "state", "stage", "repair_count", "runtime_failures",
                  "sessions", "plan", "reviews", "artifacts", "last_result", "stop_reason", "runs")
        extra = {"messages": task["messages"]} if task.get("kind") == "study" else {"guidance": task.get("guidance", [])}
        return {**{key: task[key] for key in fields}, "kind": task.get("kind", "work"), "permission": task.get("permission", "normal"), **extra,
                "harness_model": public_model(task.get("harness_model"))}
