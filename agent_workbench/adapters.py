from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .runner import redact, run_process
from .model_config import capture_model, public_model, validate_snapshot


INSTALL = Path(r"D:\applications\dsh-desktop-guanfang")
CLI = "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"


SUMMARY_LIMIT, PREVIEW_LIMIT, NOTE_LIMIT = 300, 600, 1200


def clip(text, limit):
    text = redact(str(text or "")).strip()
    return text if len(text) <= limit else text[:limit] + " …"


def tail(text, limit):
    text = redact(str(text or "")).rstrip()
    return text if len(text) <= limit else "… " + text[-limit:]


def describe_input(tool, value):
    """One readable line for a Harness tool call: the command, path or pattern it used."""
    if not isinstance(value, dict):
        return clip(value, SUMMARY_LIMIT) if value not in (None, "") else ""
    for key in ("command", "cmd", "script"):
        if isinstance(value.get(key), str):
            return clip(value[key], SUMMARY_LIMIT)
    path = next((value[k] for k in ("path", "file_path", "filePath", "file") if isinstance(value.get(k), str)), None)
    pattern = next((value[k] for k in ("pattern", "query", "glob", "regex") if isinstance(value.get(k), str)), None)
    if pattern:
        return clip(pattern + (f"  ·  {path}" if path else ""), SUMMARY_LIMIT)
    if path:
        lines = value["content"].count("\n") + 1 if isinstance(value.get("content"), str) else None
        return clip(path + (f"  ·  {lines} 行" if lines else ""), SUMMARY_LIMIT)
    return clip(json.dumps(value, ensure_ascii=False), SUMMARY_LIMIT) if value else ""


def add_usage(result, tokens_in, tokens_out, cached=None):
    usage = result.setdefault("usage", {"input": 0, "output": 0})
    usage["input"] += int(tokens_in or 0)
    usage["output"] += int(tokens_out or 0)
    if cached:
        usage["cached"] = usage.get("cached", 0) + int(cached)


def sandbox_mode(readonly, permission):
    """Both CLIs use the same preset names. Full access only when the user chose it for this task or this round."""
    if readonly:
        return "read-only"
    if permission not in {"normal", "full"}:
        raise ValueError("未知权限设置")
    return "danger-full-access" if permission == "full" else "workspace-write"


def codex_program():
    configured = os.environ.get("WORKBENCH_CODEX_EXE")
    if configured:
        return [str(Path(configured).resolve(strict=True))]
    found = shutil.which("codex.exe") or shutil.which("codex") or shutil.which("codex.cmd")
    if not found:
        raise FileNotFoundError("找不到本机 Codex CLI")
    path = Path(found)
    if path.suffix.lower() in {".cmd", ".ps1"}:
        native = list((path.parent / "node_modules/@openai/codex/node_modules").glob("@openai/codex-*/vendor/*/bin/codex.exe"))
        if native:
            return [str(native[0])]
        script = path.parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node")
        if node and script.is_file():
            return [node, str(script)]
        raise FileNotFoundError("无法解析 Codex 原生入口，请设置 WORKBENCH_CODEX_EXE")
    return [found]


class Harness:
    name = "harness"

    def __init__(self, patch=None, install=None):
        self.install = Path(install or os.environ.get("WORKBENCH_HARNESS_INSTALL", INSTALL))
        self.patch = Path(patch).resolve() if patch else None

    def capture_model(self):
        return capture_model(self.install, self.patch)

    def run(self, prompt, cwd, timeout, cancel, on_event, session_id=None, readonly=False, schema=None, model_config=None,
            permission="normal"):
        if session_id and model_config is None:
            raise ValueError("恢复 Harness 会话必须提供原任务的模型快照")
        snapshot = validate_snapshot(model_config) if model_config is not None else self.capture_model()
        with tempfile.TemporaryDirectory(prefix="workbench-model-") as temporary:
            patch = Path(temporary) / "model.patch.json"
            patch.write_text(json.dumps(snapshot["patch"], ensure_ascii=False), encoding="utf-8")
            result = self._run_with_patch(prompt, cwd, timeout, cancel, on_event, patch, session_id, sandbox_mode(readonly, permission))
        result["model"] = public_model(snapshot)
        return result

    def _run_with_patch(self, prompt, cwd, timeout, cancel, on_event, patch, session_id, mode):
        command = [str(self.install / "DeepSeek Harness.exe"), "--expose-internals", str(self.install / CLI),
                   "--profile", "headless", "--patch", str(patch), "--json",
                   "--permission-mode", mode, "--approval-policy", "never"]
        if session_id:
            command += ["--session-id", session_id]
        command += ["-"]
        env = os.environ.copy()
        env["ELECTRON_RUN_AS_NODE"] = "1"
        def parse(event, result, emit):
            kind = event.get("type")
            if kind == "thinking":
                return
            if kind == "session":
                result["session_id"] = event.get("sessionId")
                emit("session", {"session_id": result["session_id"]})
            elif kind == "final":
                result["session_id"] = event.get("sessionId") or result["session_id"]
                reason = event.get("reason", {})
                result["reason"] = reason.get("kind") if isinstance(reason, dict) else "invalid_final"
                result["final"] = redact(event.get("text", ""))
                if result["reason"] != "completed":
                    result["errors"].append(redact(json.dumps(reason, ensure_ascii=False))[:2000])
                emit("final", {"reason": result["reason"], "text": result["final"]})
            elif kind == "error":
                error = redact(event.get("message", "Harness error"))[:2000]
                result["errors"].append(error)
                emit("error", {"message": error})
            elif kind == "text":
                # Committed assistant text between tool calls (not reasoning, which is dropped above).
                if str(event.get("text", "")).strip():
                    emit("note", {"text": clip(event.get("text"), NOTE_LIMIT)})
            elif kind in {"tool_call", "tool_result", "status"}:
                safe = {k: event[k] for k in ("callId", "tool", "status", "phase") if k in event}
                if kind == "tool_call":
                    safe["summary"] = describe_input(event.get("tool"), event.get("input"))
                if kind == "tool_result":
                    if event.get("status") == "error":
                        safe["error"] = redact(str(event.get("result", "")))[:2000]
                        result["tool_errors"].append(safe["error"])
                    elif event.get("result"):
                        safe["preview"] = tail(event.get("result"), PREVIEW_LIMIT)
                if kind == "status":
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        add_usage(result, usage.get("inputTokens"), usage.get("outputTokens"), usage.get("cacheReadTokens"))
                    if event.get("phase") not in {"turn_start", "turn_end"}:
                        return  # step boundaries are noise in the process view
                emit(kind, safe)
        return run_process(command, cwd, prompt, timeout, cancel, on_event, parse, env)


class Codex:
    name = "codex"

    def run(self, prompt, cwd, timeout, cancel, on_event, session_id=None, readonly=False, schema=None, permission="normal"):
        command = codex_program() + ["-c", 'approval_policy="never"', "exec", "--json", "--skip-git-repo-check",
                                     "--sandbox", sandbox_mode(readonly, permission), "--color", "never"]
        if session_id:
            command += ["resume", session_id]
        if schema:
            command += ["--output-schema", str(schema)]
        command += ["-"]
        # CLI account, model and user configuration are inherited; no API keys are read or copied.
        def parse(event, result, emit):
            kind = event.get("type", "")
            if kind == "thread.started":
                result["session_id"] = event.get("thread_id")
                emit("session", {"session_id": result["session_id"]})
            elif kind in {"turn.failed", "error"}:
                message = redact(json.dumps(event.get("error", event.get("message", "Codex error")), ensure_ascii=False))[:2000]
                result["errors"].append(message)
                emit("error", {"message": message})
            elif kind == "turn.completed":
                result["reason"] = "completed"
                result["finished_at"] = time.monotonic()
                usage = event.get("usage", {})
                add_usage(result, usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cached_input_tokens"))
                emit("turn_completed", {"usage": usage})
            elif kind.startswith("item."):
                item = event.get("item", {})
                category = item.get("type")
                if category == "reasoning":
                    return
                if category == "agent_message" and kind == "item.completed":
                    result["final"] = redact(item.get("text", ""))
                    emit("note", {"text": clip(result["final"], NOTE_LIMIT)})
                elif category in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                    safe = {k: item[k] for k in ("id", "type", "status", "exit_code") if k in item}
                    if category == "command_execution":
                        safe["summary"] = clip(item.get("command"), SUMMARY_LIMIT)
                        if kind == "item.completed" and item.get("aggregated_output"):
                            safe["preview"] = tail(item.get("aggregated_output"), PREVIEW_LIMIT)
                    elif category == "file_change":
                        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
                        safe["summary"] = clip("，".join(f"{c.get('kind', '')} {c.get('path', '')}".strip()
                                                         for c in changes if isinstance(c, dict)), SUMMARY_LIMIT)
                    elif category == "mcp_tool_call":
                        safe["summary"] = clip(f"{item.get('server', '')}.{item.get('tool', '')}".strip("."), SUMMARY_LIMIT)
                    elif category == "web_search":
                        safe["summary"] = clip(item.get("query"), SUMMARY_LIMIT)
                    if (item.get("status") in {"failed", "error"} or item.get("error") or
                        (kind == "item.completed" and item.get("exit_code") not in (None, 0))):
                        safe["error"] = redact(str(item.get("error") or item.get("aggregated_output") or "工具失败"))[:2000]
                        result["tool_errors"].append(safe["error"])
                    emit("tool_event", safe)
        return run_process(command, cwd, prompt, timeout, cancel, on_event, parse)


def parse_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型结果必须是 JSON 对象")
    return value
