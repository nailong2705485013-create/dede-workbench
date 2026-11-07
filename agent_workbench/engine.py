from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid

from .adapters import Codex, Harness, parse_object
from .runner import redact, run_process
from .locks import InstanceLock
from .model_config import public_model, validate_snapshot
from .store import ACTIVE, now
from .workspace import HARNESS_ACL_HINT, detect_checks, harness_can_grant
from . import study
from . import files
import shutil


SCHEMA_PLAN = {"type": "object", "properties": {
    "plan": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}},
    "acceptance": {"type": "array", "items": {"type": "string"}}},
    "required": ["plan", "steps", "acceptance"], "additionalProperties": False}
SCHEMA_REVIEW = {"type": "object", "properties": {
    "passed": {"type": "boolean"}, "summary": {"type": "string"},
    "issues": {"type": "array", "items": {"type": "string"}},
    "minor_patch": {"type": "boolean"}, "verification_blocked": {"type": "boolean"}},
    "required": ["passed", "summary", "issues", "minor_patch", "verification_blocked"], "additionalProperties": False}
INFRA = ("setnamedsecurityinfow", "createrestrictedtoken", "no sandbox backend", "missing_credential",
         "sandbox backend", "connection refused", "connection reset", "network is unreachable",
         "unauthorized", "authentication", "timed out", "rate limit", "failed to spawn", "permission denied")
LIMITS = {"work": 3, "study": 3}  # concurrent rounds per lane; Study never waits behind Work
CHUNKING = ("写入较大的文件时分批写入（每次不超过约 300 行），先写骨架再逐步补充；修改已有文件时用局部编辑，"
            "不要在一次回复里输出整个大文件，以免超过单次输出长度上限。\n")
CONTINUE_PROMPT = ("你上一次回复因为单次输出长度上限被截断了。请检查工作目录里已经写好的内容，从中断处继续完成任务；"
                   "不要重复已完成的部分，把剩余内容分成小块逐步写入（每次不超过约 300 行），最后报告实际修改和验证结果。")
INTERRUPTED = object()  # a round stopped by "interrupt and send", not by the user's stop button
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".agent-workbench", ".idea", ".next"}


def inventory(root, limit=5000):
    result = {}
    count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP and not Path(current, d).is_symlink()]
        for name in files:
            path = Path(current, name)
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
                fingerprint = hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= 2_000_000 else f"{stat.st_size}:{stat.st_mtime_ns}"
                result[str(path.relative_to(root))] = fingerprint
                count += 1
            except OSError:
                continue
            if count >= limit:
                return result
    return result


class Engine:
    def __init__(self, store, patch=None, adapters=None, study_root=None):
        self.store = store
        self.study_root = Path(study_root) if study_root else study.default_root()
        self.adapters = adapters or {"codex": Codex(), "harness": Harness(patch)}
        self.pending = queue.Queue()
        self.cancel_flags = {}
        self.interrupts = set()
        self.flags_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None
        self.running = {}  # task id -> (lane, folder key)
        self.schema_dir = store.root / "schemas"
        self.schema_dir.mkdir(exist_ok=True)
        for name, schema in (("plan", SCHEMA_PLAN), ("review", SCHEMA_REVIEW)):
            (self.schema_dir / f"{name}.json").write_text(json.dumps(schema), encoding="utf-8")

    def start(self):
        self.store.recover()
        self.worker = threading.Thread(target=self._worker, daemon=True, name="workbench-dispatcher")
        self.worker.start()
        for task in reversed(self.store.list()):
            if task["state"] == "queued":
                self.pending.put(task["id"])

    def close(self):
        self.stop_event.set()
        with self.flags_lock:
            for flag in self.cancel_flags.values():
                flag.set()
        self.pending.put(None)
        if self.worker:
            self.worker.join(timeout=25)
        for thread in [t for t in threading.enumerate() if t.name.startswith("workbench-task-")]:
            thread.join(timeout=25)

    @property
    def upload_root(self):
        return self.store.root / "attachments"

    def submit(self, attachments=None, **kwargs):
        task = self.store.create(attachments=files.check_attachments(attachments, self.upload_root), **kwargs)
        self.pending.put(task["id"])
        return task

    def cancel(self, task_id):
        task = self.store.get(task_id)
        if task["state"] not in ACTIVE:
            return task
        with self.flags_lock:
            flag = self.cancel_flags.get(task_id)
            if flag:
                flag.set()
        task = self.store.update(task_id, cancel_requested=True)
        self.store.event(task_id, "cancel_requested", {})
        if task["state"] == "queued":
            task = self.store.update(task_id, state="cancelled", stop_reason="用户停止")
        return task

    def resume(self, task_id, takeover=False, full_access_once=False, auto_checks=False):
        with self.flags_lock:
            if task_id in self.cancel_flags:
                raise ValueError("旧进程尚未结束，暂时不能恢复")
            task = self.store.get(task_id)
            if task["state"] not in {"runtime_failed", "interrupted", "needs_user", "cancelled"}:
                raise ValueError("当前任务状态不能恢复")
            if takeover and task.get("kind") == "study":
                raise ValueError("Study 对话没有接管；可以直接切换模型后继续对话")
            if full_access_once and (task.get("kind") == "study" or task.get("permission", "normal") == "full"):
                raise ValueError("只有一般权限的 Work 任务可以临时提升权限")
            if task["state"] == "needs_user" and task["repair_count"] >= 3 and not takeover:
                raise ValueError("已达到三次修补上限；只有明确选择 Codex 接管才能继续")
            if takeover:
                task["takeover"] = True
                task["stage"] = "execute"
            if auto_checks:
                if task.get("kind") == "study":
                    raise ValueError("Study 对话没有验收命令")
                self.store.update(task_id, auto_checks=True, checks_stale=True)
            self.store.update(task_id, state="queued", cancel_requested=False, stop_reason=None,
                              takeover=task["takeover"], stage=task["stage"], escalate=bool(full_access_once))
            self.store.event(task_id, "takeover_requested" if takeover else "resume_requested",
                             {"full_access_once": True} if full_access_once else {})
            self.pending.put(task_id)
        return self.store.get(task_id)

    def study_ready(self):
        root = self.study_root
        return root.is_dir() and harness_can_grant(root)

    def setup_study_root(self):
        study.grant_full_control(self.study_root)
        if not self.study_ready():
            raise ValueError("Study 文件夹授权后仍不满足要求；请检查 " + str(self.study_root) + " 的“完全控制”权限")
        return self.study_root

    @staticmethod
    def _chat_args(text, executor):
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("消息不能为空，且最多 32000 字符")
        if executor not in study.EXECUTORS:
            raise ValueError("未知模型；可选 Harness 或 Codex")
        return text.strip()

    def submit_study(self, text, executor="harness", attachments=None):
        text = self._chat_args(text, executor)
        attachments = files.check_attachments(attachments, self.upload_root)
        if not self.study_ready():
            raise ValueError("Study 文件夹还没有授权，请先在页面上同意设置 " + str(self.study_root))
        task = self.store.create_study(text, executor, study.make_folder(self.study_root), attachments)
        self.pending.put(task["id"])
        return task

    def add_message(self, task_id, text, executor=None, interrupt=False, attachments=None):
        """Study: next chat message. Work: guidance. While a round runs it waits for the round, or interrupts it."""
        attachments = files.check_attachments(attachments, self.upload_root)
        if self.store.get(task_id).get("kind") != "study":
            return self.guide(task_id, text, interrupt, attachments)
        text = self._chat_args(text, executor)
        with self.flags_lock:
            task = self.store.get(task_id)
            if not Path(task["cwd"]).is_dir():
                raise ValueError("这个对话的文件夹已不存在，请新建对话")
            messages = task["messages"] + [dict(role="user", text=text, at=now(), attachments=attachments)]
            if task["state"] in ACTIVE:
                self.store.update(task_id, messages=messages, executor=executor)
                if interrupt:
                    self._interrupt(task_id)
            else:
                self.store.update(task_id, messages=messages, executor=executor, state="queued",
                                  cancel_requested=False, stop_reason=None)
                self.pending.put(task_id)
            self.store.event(task_id, "message", {"executor": executor, "queued": task["state"] in ACTIVE,
                                                  "interrupt": bool(interrupt)})
        return self.store.get(task_id)

    def guide(self, task_id, text, interrupt=False, attachments=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise ValueError("引导内容不能为空，且最多 32000 字符")
        with self.flags_lock:
            task = self.store.get(task_id)
            guidance = task.get("guidance", []) + [dict(text=text.strip(), at=now(), interrupt=bool(interrupt), applied=False,
                                                        attachments=attachments or [])]
            if task["state"] in ACTIVE:
                self.store.update(task_id, guidance=guidance)
                if interrupt:
                    self._interrupt(task_id)
            else:
                # Continue the same task. Guidance rounds never count toward the three quality repairs.
                stage = "plan" if task["stage"] == "plan" and not task.get("plan") else "guide"
                self.store.update(task_id, guidance=guidance, state="queued", stage=stage,
                                  cancel_requested=False, stop_reason=None)
                self.pending.put(task_id)
            self.store.event(task_id, "guidance", {"queued": task["state"] in ACTIVE, "interrupt": bool(interrupt)})
        return self.store.get(task_id)

    def _interrupt(self, task_id):
        """Caller holds flags_lock. Stops the running round only; the task then continues with the new input."""
        flag = self.cancel_flags.get(task_id)
        if flag:
            self.interrupts.add(task_id)
            flag.set()

    def _take_interrupt(self, task_id, cancel):
        with self.flags_lock:
            if task_id not in self.interrupts:
                return False
            self.interrupts.discard(task_id)
            if self.store.get(task_id)["cancel_requested"]:
                return False  # the stop button wins
            cancel.clear()
        self.store.event(task_id, "interrupted_for_input", {})
        return True

    def _should_stop(self, task_id, cancel):
        if not (cancel.is_set() or self.store.get(task_id)["cancel_requested"]):
            return False
        return not self._take_interrupt(task_id, cancel)

    def _apply_guidance(self, task_id):
        task = self.store.get(task_id)
        fresh = [g for g in task.get("guidance", []) if not g["applied"]]
        if fresh:
            self.store.update(task_id, guidance=[{**g, "applied": True} for g in task["guidance"]])
        return fresh

    @staticmethod
    def _has_pending_guidance(task):
        return any(not g["applied"] for g in task.get("guidance", []))

    def delete(self, task_id):
        with self.flags_lock:
            task = self.store.get(task_id)
            if task_id in self.cancel_flags or task["state"] in ACTIVE:
                raise ValueError("任务正在运行或排队，请先停止再删除")
            if task.get("kind") == "study":
                study.remove_folder(task["cwd"], self.study_root)
            files.remove_uploads(files.task_attachments(task), self.upload_root)
            shutil.rmtree(self.store.root / "runs" / task_id, ignore_errors=True)
            self.store.delete(task_id)
        return {"deleted": task_id}

    def _run_study(self, task_id, cancel):
        while self._study_turn(task_id, cancel):
            pass

    def _study_turn(self, task_id, cancel):
        """One reply. Returns True when another turn is needed (messages arrived meanwhile, or an interrupt)."""
        task = self.store.get(task_id)
        executor = task["executor"]
        try:
            if not Path(task["cwd"]).is_dir():
                raise ValueError("这个对话的文件夹已不存在")
            if executor == "harness":
                if not harness_can_grant(task["cwd"]):
                    raise ValueError(HARNESS_ACL_HINT)
                if task.get("harness_model") is not None:
                    validate_snapshot(task["harness_model"])
                else:
                    snapshot = self.adapters["harness"].capture_model()
                    validate_snapshot(snapshot)
                    self.store.update(task_id, harness_model=snapshot)
                    self.store.event(task_id, "model_configured", {"executor": "harness", **public_model(snapshot)})
        except (OSError, ValueError) as exc:
            self._runtime_failure(task_id, str(exc))
            return False
        if self._should_stop(task_id, cancel):
            self._transition(task_id, "cancelled", stop_reason="用户停止")
            return False
        task = self._transition(task_id, "executing")
        count = len(task["messages"])
        fresh = not task["sessions"].get(executor + ":chat")
        prompt = study.build_prompt(task, executor, fresh=fresh)
        result = self._run(task, executor, "chat", prompt, cancel, images=study.images(study.unseen(task, executor, fresh)))
        if result is INTERRUPTED:
            return True
        if result is None:
            return False
        latest = self.store.get(task_id)
        reply = dict(role="assistant", executor=executor, text=result.get("final") or "（没有返回文本）",
                     at=now(), run=latest["runs"][-1]["id"])
        if result.get("tool_errors"):
            reply["tool_errors"] = result["tool_errors"][:5]
        # Messages sent while this reply was being written go after it and become the next turn.
        later = latest["messages"][count:]
        messages = latest["messages"][:count] + [reply] + later
        self._transition(task_id, "executing" if later else "passed", messages=messages,
                         seen={**latest["seen"], executor: count + 1}, stop_reason=None)
        try:
            study.write_transcript(self.store.get(task_id))
        except OSError:
            pass
        return bool(later)

    def _worker(self):
        """Dispatcher: Work runs up to 3 at once, Study in its own lane, never two tasks in one folder."""
        waiting = []
        while not self.stop_event.is_set():
            try:
                item = self.pending.get(timeout=.2)
                while True:
                    if item is None:
                        return
                    if item not in waiting:
                        waiting.append(item)
                    item = self.pending.get_nowait()
            except queue.Empty:
                pass
            for task_id in list(waiting):
                with self.flags_lock:
                    try:
                        task = self.store.get(task_id)
                    except KeyError:  # deleted while queued
                        waiting.remove(task_id)
                        continue
                    if task["state"] != "queued" or task_id in self.cancel_flags:
                        waiting.remove(task_id)
                        continue
                    lane = "study" if task.get("kind") == "study" else "work"
                    folder = os.path.normcase(str(Path(task["cwd"]).resolve()))
                    busy = [v for v in self.running.values() if v[0] == lane]
                    if len(busy) >= LIMITS[lane] or any(v[1] == folder for v in self.running.values()):
                        continue  # stays queued until a slot or the folder frees up
                    waiting.remove(task_id)
                    cancel = self.cancel_flags[task_id] = threading.Event()
                    self.running[task_id] = (lane, folder)
                threading.Thread(target=self._execute, args=(task_id, cancel), daemon=True,
                                 name="workbench-task-" + task_id).start()

    def _execute(self, task_id, cancel):
        try:
            self.run_task(task_id, cancel)
        except Exception as exc:
            self._runtime_failure(task_id, "工作台内部错误: " + redact(str(exc)))
        finally:
            with self.flags_lock:
                self.cancel_flags.pop(task_id, None)
                self.running.pop(task_id, None)

    def scheduler_state(self):
        with self.flags_lock:
            lanes = [v[0] for v in self.running.values()]
        return {"work": lanes.count("work"), "study": lanes.count("study"), "limits": LIMITS}

    def _transition(self, task_id, state, **kwargs):
        task = self.store.update(task_id, state=state, **kwargs)
        self.store.event(task_id, "state", {"state": state, "stage": task["stage"], "repair_count": task["repair_count"]})
        return task

    def _runtime_failure(self, task_id, reason):
        task = self.store.get(task_id)
        self._transition(task_id, "runtime_failed", stop_reason=reason,
                         runtime_failures=task["runtime_failures"] + 1)

    def _run(self, task, executor, role, prompt, cancel, readonly=False, schema=None, continued=None, images=None):
        task_id = task["id"]
        key = f"{executor}:{role}"
        session = task["sessions"].get(key)
        # Planner and reviewer stay read-only; full access is the task's setting or a one-round escalation.
        permission = continued or ("full" if not readonly and (task.get("permission") == "full" or task.get("escalate")) else "normal")
        if task.get("escalate") and not readonly:
            self.store.update(task_id, escalate=False)
            self.store.event(task_id, "full_access_round", {"executor": executor, "role": role})
        run_id = uuid.uuid4().hex[:10]
        log_dir = self.store.root / "runs" / task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{run_id}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            def emit(kind, data):
                if kind == "session" and data.get("session_id"):
                    sessions = self.store.get(task_id)["sessions"]
                    sessions[key] = data["session_id"]
                    self.store.update(task_id, sessions=sessions)
                safe = {"executor": executor, "role": role, "run": run_id, **data}
                stream.write(json.dumps({"at": now(), "kind": kind, "data": safe}, ensure_ascii=False) + "\n")
                stream.flush()
                self.store.event(task_id, kind, safe)
            try:
                if images is None:  # Work: the task's pictures go to each role's new session once
                    images = [a["path"] for a in task.get("attachments", []) if a.get("image")] if not session else []
                images = [p for p in images if Path(p).is_file()]
                extra = {"model_config": task.get("harness_model")} if executor == "harness" else {"images": images}
                result = self.adapters[executor].run(prompt, task["cwd"], task["timeout"], cancel, emit, session_id=session,
                                                     readonly=readonly, schema=schema, permission=permission, **extra)
            except (OSError, ValueError) as exc:
                result = dict(ok=False, transport_ok=False, final=None, session_id=None,
                              errors=[redact(str(exc))], tool_errors=[], reason="startup_failed")
        latest = self.store.get(task_id)
        if result.get("session_id"):
            latest["sessions"][key] = result["session_id"]
        run = {"id": run_id, "executor": executor, "role": role, "at": now(), "log": str(path), "result": result,
               "permission": "read-only" if readonly else permission, "stage": task.get("stage"),
               "auto_continue": bool(continued)}
        self.store.update(task_id, runs=latest["runs"] + [run], sessions=latest["sessions"], last_result=result)
        (log_dir / f"{run_id}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.get("cancelled"):
            if self._take_interrupt(task_id, cancel):
                runs = self.store.get(task_id)["runs"]
                runs[-1]["interrupted"] = True
                self.store.update(task_id, runs=runs)
                return INTERRUPTED
            self._transition(task_id, "cancelled", stop_reason="用户停止；可显式恢复")
            return None
        if self._should_stop(task_id, cancel):  # an interrupt that came after the process finished keeps its result
            self._transition(task_id, "cancelled", stop_reason="用户停止；可显式恢复")
            return None
        if (result.get("reason") == "max-tokens" and not continued and not readonly
                and self.store.get(task_id)["sessions"].get(key)):
            # Output-length cut-off: continue once in the same session, in small pieces. A second cut-off pauses.
            self.store.event(task_id, "auto_continue", {"executor": executor, "role": role, "reason": "max-tokens"})
            return self._run(self.store.get(task_id), executor, role, CONTINUE_PROMPT, cancel, readonly, schema,
                             continued=permission)
        tool_errors = result.get("tool_errors", [])
        infrastructure = any(word in " ".join(tool_errors).lower() for word in INFRA)
        if not result.get("transport_ok", result["ok"]) or infrastructure:
            diagnostic = next((line for line in result.get("stderr_tail", []) if "error" in line.lower() or "EPERM" in line), "")
            reason = "; ".join(result.get("errors", []) + tool_errors) or diagnostic or ("单轮超时" if result.get("timed_out") else "进程未正常完成或缺少完成事件")
            if "setnamedsecurityinfow" in reason.lower():
                reason = HARNESS_ACL_HINT + "\n" + reason
            if result.get("reason") == "max-tokens":
                reason = ("模型单次输出超过长度上限（思考和回答共用输出额度）" + ("，自动续写 1 次后仍被截断" if continued else "")
                          + "。建议发一句引导让它拆小步骤、分文件写，再恢复；也可以在 Harness 桌面端调高输出上限或降低推理强度。\n" + reason)
            self._runtime_failure(task_id, reason[:4000])
            return None
        return result

    def _checks(self, task, cancel):
        results = []
        auto = not task["checks"] and task.get("auto_checks", False)
        for argv in task["checks"] or (detect_checks(task["cwd"]) if auto else []):
            if cancel.is_set():
                break
            # These are explicit user-supplied local commands, never generated from model text.
            # Use the same bounded lifecycle runner, with a JSON-emitting Python shim around argv.
            import sys
            shim = ("import subprocess,sys,json; "
                    "p=subprocess.run(json.loads(sys.argv[1]),stdout=subprocess.PIPE,stderr=subprocess.STDOUT); "
                    "print(json.dumps({'type':'check','exit_code':p.returncode,'output':p.stdout.decode('utf-8',errors='replace')[-6000:]})); "
                    "sys.exit(p.returncode)")
            def consume(event, result, emit):
                result["final"] = redact(event.get("output", ""))
                result["reason"] = "completed"
            result = run_process([sys.executable, "-c", shim, json.dumps(argv)], task["cwd"], "",
                                 min(task["timeout"], 300), cancel, lambda *_: None, consume)
            check = {"argv": argv, "auto": auto, "passed": result["ok"], "exit_code": result["exit_code"],
                     "output": result["final"], "errors": result["errors"],
                     "timed_out": result["timed_out"], "cancelled": result["cancelled"],
                     "runtime_error": result["timed_out"] or bool(result["errors"]) or result["reason"] != "completed"}
            results.append(check)
            self.store.event(task["id"], "acceptance_check", check)
        return results

    def _artifacts(self, task):
        current = inventory(Path(task["cwd"]))
        baseline = task.get("baseline") or {}
        artifacts = [{"path": name, "change": "deleted" if name not in current else "created" if name not in baseline else "modified"}
                     for name in sorted(set(baseline) | set(current)) if baseline.get(name) != current.get(name)]
        self.store.update(task["id"], artifacts=artifacts)
        return artifacts

    def run_task(self, task_id, cancel=None):
        cwd = self.store.get(task_id)["cwd"]
        canonical = os.path.normcase(str(Path(cwd).resolve()))
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        lock_path = Path(tempfile.gettempdir()) / "local-agent-workbench-locks" / (key + ".lock")
        try:
            with InstanceLock(lock_path, "这个工作目录已有另一项工作台任务在运行；结束后可恢复"):
                self._run_task(task_id, cancel)
        except RuntimeError as exc:
            self._runtime_failure(task_id, str(exc))

    def _run_task(self, task_id, cancel=None):
        cancel = cancel or threading.Event()
        task = self.store.get(task_id)
        if task.get("kind") == "study":
            return self._run_study(task_id, cancel)
        if task["mode"] in {"harness", "combined"} and not task["takeover"]:
            try:
                if task.get("permission", "normal") == "normal" and not task.get("escalate") and not harness_can_grant(task["cwd"]):
                    raise ValueError(HARNESS_ACL_HINT)
                if task.get("harness_model") is not None:
                    validate_snapshot(task["harness_model"])
                else:
                    if task.get("sessions", {}).get("harness:executor"):
                        raise ValueError("旧任务没有模型快照；为避免改变原通道，请新建任务或明确选择 Codex 接管。")
                    snapshot = self.adapters["harness"].capture_model()
                    validate_snapshot(snapshot)
                    self.store.update(task_id, harness_model=snapshot)
                    self.store.event(task_id, "model_configured", {"executor": "harness", **public_model(snapshot)})
            except (OSError, ValueError) as exc:
                self._runtime_failure(task_id, str(exc))
                return
        if task["baseline"] is None:
            self.store.update(task_id, baseline=inventory(Path(task["cwd"])))
        while True:
            task = self.store.get(task_id)
            if self._should_stop(task_id, cancel):
                self._transition(task_id, "cancelled", stop_reason="用户停止")
                return
            if task["stage"] == "review" and self._has_pending_guidance(task):
                task = self.store.update(task_id, stage="guide")
            stage = task["stage"]
            context = ("当前任务仅限工作目录。不要发送外部消息、部署或修改其他目录。"
                       "不要自行关闭沙箱、修改 ACL、接管其他 GUI 会话或在后台遗留进程。\n"
                       f"用户任务：{task['prompt']}" + files.prompt_note(task.get("attachments")) + "\n")
            if task.get("guidance"):
                context += ("用户在过程中的补充与纠正（按时间顺序，后面的优先）：\n"
                            + "\n".join(f"{n}. {g['text']}" for n, g in enumerate(task["guidance"], 1)) + "\n")
            if stage == "plan":
                self._transition(task_id, "planning")
                result = self._run(task, "codex", "planner", context +
                    "你是规划者。先检查项目，给出可执行计划和验收条件。不要修改文件。只输出符合 schema 的 JSON。",
                    cancel, readonly=True, schema=self.schema_dir / "plan.json")
                if result is INTERRUPTED:
                    continue
                if result is None:
                    return
                try:
                    plan = parse_object(result["final"])
                    if not isinstance(plan.get("plan"), str) or not isinstance(plan.get("steps"), list) or not isinstance(plan.get("acceptance"), list):
                        raise ValueError("缺少有效计划或验收条件")
                except (ValueError, TypeError) as exc:
                    self._runtime_failure(task_id, "规划结果协议错误: " + str(exc))
                    return
                self.store.update(task_id, plan=plan, stage="execute")
                continue
            if stage in {"execute", "repair", "patch", "guide"}:
                executor = "codex" if task["takeover"] or task["mode"] == "codex" or stage == "patch" else "harness"
                self._transition(task_id, "patching" if stage == "patch" else "repairing" if stage == "repair" else "executing")
                instruction = context + "\n实施已确认的任务，运行适当验证；报告实际修改、验证结果及限制。\n" + CHUNKING
                if task["plan"]:
                    instruction += "计划与验收：" + json.dumps(task["plan"], ensure_ascii=False) + "\n"
                if stage in {"repair", "patch"} and task["reviews"]:
                    instruction += "本轮只修复审核指出的问题：" + json.dumps(task["reviews"][-1], ensure_ascii=False) + "\n"
                fresh = self._apply_guidance(task_id)
                if fresh:
                    instruction += ("用户刚刚补充或纠正了要求，请在现有成果的基础上照此调整：\n"
                                    + "\n".join("- " + g["text"] + files.prompt_note(g.get("attachments")) for g in fresh) + "\n")
                latest = self.store.get(task_id)
                pictures = [a["path"] for g in fresh for a in g.get("attachments", []) if a.get("image")]
                if not latest["sessions"].get(executor + ":executor"):
                    pictures = [a["path"] for a in latest.get("attachments", []) if a.get("image")] + pictures
                result = self._run(latest, executor, "executor", instruction, cancel, images=pictures)
                self._artifacts(self.store.get(task_id))
                if result is INTERRUPTED:
                    self.store.update(task_id, stage="guide")
                    continue
                if result is None:
                    return
                checks = self._checks(self.store.get(task_id), cancel)
                self.store.update(task_id, last_checks=checks)
                if self._should_stop(task_id, cancel):
                    self._transition(task_id, "cancelled", stop_reason="用户停止")
                    return
                if self._has_pending_guidance(self.store.get(task_id)):
                    self.store.update(task_id, stage="guide")
                    continue
                if any(c["runtime_error"] for c in checks):
                    self._runtime_failure(task_id, "验收命令启动失败或超时；检查命令及运行环境后恢复。")
                    return
                if task["mode"] != "combined" or task["takeover"]:
                    # With test results, they decide; without them, a tool error mid-run is the only signal left.
                    passed = all(c["passed"] for c in checks) if checks else not result.get("tool_errors")
                    reason = (("单执行器已完成，验收命令通过；未经过独立 Codex 审核" if checks else "单执行器已完成；未经过独立 Codex 审核")
                              if passed else ("验收命令未通过，请检查结果" if checks else
                                              "执行中出现工具错误，且没有验收命令可以确认结果，请检查结果"))
                    self._transition(task_id, "passed" if passed else "needs_user", stage="execute", stop_reason=reason)
                    return
                self.store.update(task_id, stage="review")
                continue
            if stage == "review":
                if task.get("checks_stale"):
                    checks = self._checks(task, cancel)
                    task = self.store.update(task_id, last_checks=checks, checks_stale=False)
                    if any(c["runtime_error"] for c in checks):
                        self._runtime_failure(task_id, "验收命令启动失败或超时；检查命令及运行环境后恢复。")
                        return
                self._transition(task_id, "reviewing")
                execution = task.get("last_execution") or next((r["result"] for r in reversed(task["runs"]) if r["role"] == "executor"), {})
                checks = task.get("last_checks", [])
                evidence = {"plan": task["plan"], "artifacts": task["artifacts"], "repair_count": task["repair_count"],
                            "execution_report": execution.get("final"),
                            "intermediate_tool_errors": execution.get("tool_errors", [])[:5],
                            "checks_run_by_workbench": [{k: c.get(k) for k in ("argv", "auto", "passed", "exit_code", "output")} for c in checks]}
                result = self._run(task, "codex", "reviewer", context +
                    "你是独立审核者。亲自检查工作目录中的实际成果并运行必要的只读检查。不要修改文件。"
                    "不能仅凭执行者自述判定通过；未满足验收条件时不得通过。"
                    "checks_run_by_workbench 是工作台在沙箱外实际运行的验收命令及输出，可作为测试结果的依据。"
                    "intermediate_tool_errors 只是执行过程中的工具报错，执行者可能已重试修复，请以工作目录中的实际成果为准，不要仅因它判不通过。"
                    "如果没有发现成果本身的问题，只是当前只读环境无法独立验证（例如测试需要写临时文件、而 checks 没有覆盖），"
                    "设 verification_blocked=true、passed=false，并在 issues 中说明需要什么验收命令；发现实际问题时 verification_blocked=false。"
                    "只有少量局部修改可解决时 minor_patch=true；不能用它要求全面重写。只输出符合 schema 的 JSON。\n"
                    + json.dumps(evidence, ensure_ascii=False), cancel, readonly=True, schema=self.schema_dir / "review.json")
                if result is INTERRUPTED:
                    self.store.update(task_id, stage="guide")
                    continue
                if result is None:
                    return
                try:
                    review = parse_object(result["final"])
                    review.setdefault("verification_blocked", False)
                    if (type(review.get("passed")) is not bool or type(review.get("minor_patch")) is not bool
                            or type(review["verification_blocked"]) is not bool
                            or not isinstance(review.get("issues"), list) or not isinstance(review.get("summary"), str)):
                        raise ValueError("审核字段无效")
                except (ValueError, TypeError) as exc:
                    self._runtime_failure(task_id, "审核结果协议错误: " + str(exc))
                    return
                failed = [c for c in checks if not c["passed"]]
                if failed:  # objective results always override the reviewer
                    review.update(passed=False, verification_blocked=False)
                    review["issues"].append("验收命令未通过：" + "；".join(" ".join(c["argv"][-4:]) + f"（退出码 {c['exit_code']}）" for c in failed))
                if review["passed"]:
                    review["verification_blocked"] = False
                review["at"] = now()
                reviews = task["reviews"] + [review]
                self.store.update(task_id, reviews=reviews)
                self.store.event(task_id, "review", review)
                if self._has_pending_guidance(self.store.get(task_id)):
                    self.store.update(task_id, stage="guide")
                    continue
                if review["passed"]:
                    self._transition(task_id, "passed", stop_reason=None)
                    return
                if review["verification_blocked"]:
                    # The executor cannot fix the reviewer's environment, so this does not consume a repair.
                    self._transition(task_id, "needs_user", stage="review", stop_reason=(
                        "审核者没有发现成果本身的问题，但在当前环境里无法独立验证：" + "；".join(review["issues"])[:1500]
                        + "\n可以开启自动验收（或填写验收命令）后重新审核；这不计入修补次数。"))
                    return
                if task["repair_count"] >= 3:
                    self._transition(task_id, "needs_user", stop_reason="第 3 次修补后审核仍未通过；已停止，等待用户处理或明确要求 Codex 接管。")
                    return
                self.store.update(task_id, repair_count=task["repair_count"] + 1,
                                  stage="patch" if review["minor_patch"] else "repair")
                continue
            raise ValueError("未知任务阶段: " + stage)
