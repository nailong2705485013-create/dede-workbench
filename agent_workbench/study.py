"""Study conversations: lightweight one-agent chat in its own folder under the study root."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import shutil
import stat
import subprocess

EXECUTORS = {"harness", "codex"}
NAMES = {"harness": "Harness", "codex": "Codex"}
TRANSCRIPT = "对话记录.md"
CONTEXT_LIMIT = 40000
RULES = ("你正在 dede工作台 的 Study 对话中回答用户。工作目录就是本对话的文件夹；"
         "需要产出文件（文档、整理结果、小脚本）时写到 output 子文件夹。"
         "不要修改工作目录以外的文件，不要部署、安装软件或发送外部消息。"
         "写较大的文件时分批写入（每次不超过约 300 行）。直接、简洁地回答。\n\n")


def default_root():
    configured = os.environ.get("WORKBENCH_STUDY_ROOT")
    if configured:
        return Path(configured)
    return Path("D:/dede工作台") if Path("D:/").is_dir() else Path.home() / "dede工作台"


def study_dir(root):
    return Path(root) / "study"


def make_folder(root):
    """Create this conversation's folder, named by creation time, with an output subfolder."""
    parent = study_dir(root)
    parent.mkdir(parents=True, exist_ok=True)
    name = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path, n = parent / name, 1
    while path.exists():
        n += 1
        path = parent / f"{name}_{n}"
    (path / "output").mkdir(parents=True)
    return path.resolve()


def grant_full_control(root):
    """Option A, run only after the user agrees: create the root and give the signed-in user inheritable full control."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        return
    flags = subprocess.CREATE_NO_WINDOW
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=15, creationflags=flags)
    sid = who.stdout.decode("utf-8", "replace").strip().split(",")[-1].strip('"')
    if not sid.startswith("S-1-"):
        raise ValueError("无法确定当前用户账户")
    done = subprocess.run(["icacls", str(root), "/grant", f"*{sid}:(OI)(CI)F"], capture_output=True,
                          timeout=60, creationflags=flags)
    if done.returncode != 0:
        raise ValueError("设置文件夹权限失败；请在文件夹“属性 → 安全”中手动给自己的账户加“完全控制”")


def remove_folder(path, root):
    """Permanently delete one conversation folder, only if it sits directly inside the study directory."""
    path = Path(path)
    if not path.exists():
        return
    parent = study_dir(root).resolve()
    if path.is_symlink() or os.path.isjunction(path) or path.resolve().parent != parent:
        raise ValueError("对话文件夹不在 Study 目录中，已拒绝删除")

    def writable(func, target, _):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    shutil.rmtree(path, onexc=writable)


def label(message):
    return "用户" if message["role"] == "user" else NAMES.get(message.get("executor"), "助手")


def transcript(task):
    lines = [f"# {task['prompt'].splitlines()[0][:80]}\n", f"文件夹：{task['cwd']}\n"]
    for m in task["messages"]:
        lines.append(f"\n## {label(m)} · {m['at']}\n\n{m['text']}\n")
    return "".join(lines)


def write_transcript(task):
    Path(task["cwd"], TRANSCRIPT).write_text(transcript(task), encoding="utf-8")


def build_prompt(task, executor, fresh):
    """The new session gets the rules; every turn gets the messages this executor has not seen yet."""
    messages = task["messages"]
    batch = messages[0 if fresh else task["seen"].get(executor, 0):]
    head = RULES if fresh else ""
    if all(m["role"] == "user" for m in batch):
        return head + "\n\n".join(m["text"] for m in batch)
    *history, = batch
    while history and history[-1]["role"] == "user":
        history.pop()
    latest = batch[len(history):]
    blocks, size = [], 0
    for m in reversed(history):
        block = f"### {label(m)}\n{m['text']}\n"
        if size + len(block) > CONTEXT_LIMIT:
            blocks.append(f"（更早的记录较长，已省略；完整记录在工作目录的 {TRANSCRIPT}，需要时可以读取。）\n")
            break
        blocks.append(block)
        size += len(block)
    return (head + "以下是这段对话中你还没看到的记录，可能来自其他模型，请据此接着对话：\n\n"
            + "\n".join(reversed(blocks)) + "\n---\n用户的最新消息：\n" + "\n\n".join(m["text"] for m in latest))


def codex_model():
    """Model name from the Codex CLI config, for display only; nothing else is read."""
    import tomllib
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"model": None, "reasoning_effort": None}
    model, effort = config.get("model"), config.get("model_reasoning_effort")
    return {"model": model if isinstance(model, str) else None,
            "reasoning_effort": effort if isinstance(effort, str) else None}
