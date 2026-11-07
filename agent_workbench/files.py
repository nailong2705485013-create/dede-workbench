"""Artifacts and attachments: list, preview, open on this machine, and upload. Paths are always checked first."""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".webp": "image/webp", ".bmp": "image/bmp", ".svg": "image/svg+xml"}
EXECUTABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi",
              ".scr", ".lnk", ".reg", ".hta", ".cpl", ".pif", ".jar"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".agent-workbench"}
MAX_UPLOAD = 20 * 1024 * 1024
MAX_PREVIEW = 15 * 1024 * 1024
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def allowed(task, path, attachments_root):
    """A file may be touched only if it is in the task's folder or is one of the task's own attachments."""
    path = Path(path)
    if not path.is_absolute():
        path = Path(task["cwd"]) / path
    path = path.resolve()
    if inside(path, task["cwd"]):
        return path
    # Attachments the user uploaded or picked with the file dialog for this task.
    if str(path) in {str(Path(a["path"]).resolve()) for a in task_attachments(task)}:
        return path
    raise ValueError("只能打开这个任务文件夹里的文件或它的附件")


def task_attachments(task):
    items = []
    for m in task.get("messages", []) + task.get("guidance", []):
        items += m.get("attachments", [])
    return items + task.get("attachments", [])


def describe(path, root=None):
    path = Path(path)
    stat = path.stat()
    suffix = path.suffix.lower()
    return {"path": str(path), "rel": str(path.relative_to(root)) if root else path.name, "name": path.name,
            "size": stat.st_size, "mtime": stat.st_mtime, "image": suffix in IMAGES, "executable": suffix in EXECUTABLE}


GROUP_AT = 20
REAL_DOTFILES = {".gitignore", ".gitattributes", ".editorconfig", ".env", ".env.example", ".github", ".vscode",
                 ".prettierrc", ".eslintrc", ".eslintrc.json", ".npmrc", ".nvmrc", ".python-version", ".dockerignore"}


def is_temp(rel):
    """Leftovers of the agent's own checks: hidden folders/files (browser profiles, caches) and single-underscore
    files such as _shot.png or _verify.mjs. Dunder names like __init__.py are real code."""
    top = Path(rel).parts[0]
    if top.startswith(".") and top not in REAL_DOTFILES:
        return True
    name = Path(rel).name
    return len(Path(rel).parts) == 1 and name.startswith("_") and not name.startswith("__")


def group(entries, root):
    """Fold a top-level folder into one card when it is hidden (.name) or holds many files, e.g. a browser profile."""
    by_top = {}
    for e in entries:
        parts = Path(e["rel"]).parts
        if len(parts) > 1:
            by_top.setdefault(parts[0], []).append(e)
    folded = {top for top, items in by_top.items() if top.startswith(".") or len(items) >= GROUP_AT}
    out = [{**e, "temporary": is_temp(e["rel"])} for e in entries
           if Path(e["rel"]).parts[0] not in folded or len(Path(e["rel"]).parts) == 1]
    for top in sorted(folded):
        items = by_top[top]
        changes = {e["change"] for e in items}
        out.insert(0, {"path": str(Path(root) / top), "rel": top + "\\", "name": top, "folder": True, "count": len(items),
                       "size": sum(e["size"] or 0 for e in items), "image": False, "executable": False,
                       "change": changes.pop() if len(changes) == 1 else "modified", "temporary": is_temp(top + "/x")})
    return out


def list_files(task):
    """Work: the task's created/modified files. Study: everything the chat produced in its folder."""
    root = Path(task["cwd"])
    out = []
    if task.get("kind") == "study":
        for current, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in names:
                path = Path(current, name)
                if path.name == "对话记录.md" and path.parent == root:
                    continue
                try:
                    out.append({**describe(path, root), "change": "created"})
                except OSError:
                    continue
                if len(out) >= 3000:
                    break
        return group(sorted(out, key=lambda f: -f["mtime"]), root)
    for item in task.get("artifacts", []):
        path = root / item["path"]
        if item["change"] == "deleted" or not path.is_file():
            out.append({"path": str(path), "rel": item["path"], "name": Path(item["path"]).name, "size": None,
                        "image": False, "executable": False, "change": item["change"]})
            continue
        try:
            out.append({**describe(path, root), "change": item["change"]})
        except OSError:
            continue
    return group(out, root)


def clean_temp(task):
    """Delete leftovers this task created. Files that existed before the task (its baseline) are never touched."""
    root = Path(task["cwd"]).resolve()
    baseline = task.get("baseline") or {}
    removed = 0
    for item in task.get("artifacts", []):
        rel = item["path"]
        if item["change"] != "created" or rel in baseline or not is_temp(rel):
            continue
        path = (root / rel).resolve()
        if not inside(path, root) or path == root or not path.is_file():
            continue
        path.unlink()
        removed += 1
        parent = path.parent
        while parent != root and inside(parent, root) and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return removed


def read_preview(task, path, attachments_root):
    path = allowed(task, path, attachments_root)
    kind = IMAGES.get(path.suffix.lower())
    if not kind:
        raise ValueError("只能预览图片文件")
    if path.stat().st_size > MAX_PREVIEW:
        raise ValueError("图片太大，无法预览")
    return path.read_bytes(), kind


def open_path(task, path, action, attachments_root, confirmed_run=False):
    """open: default program (executables are only revealed unless the user confirmed running them);
    with: Windows 'Open with' dialog; reveal: Explorer with the file selected; folder: the task folder."""
    if action == "folder":
        target = Path(task["cwd"]).resolve()
        if not target.is_dir():
            raise ValueError("文件夹已不存在")
        _start(target)
        return {"done": "folder"}
    path = allowed(task, path, attachments_root)
    if not path.exists():
        raise ValueError("文件已不存在")
    if path.is_dir() and action in {"open", "with"}:
        _start(path)  # a folder card opens in Explorer
        return {"done": "folder"}
    if action == "reveal":
        subprocess.Popen(["explorer", "/select,", str(path)], creationflags=NO_WINDOW)
        return {"done": "reveal"}
    if action == "with":
        subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(path)], creationflags=NO_WINDOW)
        return {"done": "with"}
    if action == "open":
        if path.suffix.lower() in EXECUTABLE and not confirmed_run:
            subprocess.Popen(["explorer", "/select,", str(path)], creationflags=NO_WINDOW)
            return {"done": "reveal", "blocked": True}
        _start(path)
        return {"done": "open"}
    raise ValueError("未知操作")


def _start(path):
    if os.name == "nt":
        os.startfile(str(path))  # the user's default program for this file type
    else:
        subprocess.Popen(["xdg-open", str(path)])


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(str(name or "file")).name).strip(" .") or "file"
    return name[:120]


def save_upload(root, name, data):
    if not data or len(data) > MAX_UPLOAD:
        raise ValueError("附件为空或超过 20 MB")
    folder = Path(root) / uuid.uuid4().hex[:12]
    folder.mkdir(parents=True)
    path = folder / safe_name(name)
    path.write_bytes(data)
    return attachment(path, uploaded=True)


def attachment(path, uploaded=False):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("附件必须是文件")
    info = describe(path)
    return {"path": str(path), "name": info["name"], "size": info["size"], "image": info["image"], "uploaded": uploaded}


def check_attachments(items, upload_root):
    """Accept only files that exist; uploaded ones must live in our upload folder."""
    if items in (None, []):
        return []
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError("最多 10 个附件")
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("附件格式无效")
        info = attachment(item["path"], uploaded=inside(item["path"], upload_root))
        out.append(info)
    return out


def remove_uploads(items, upload_root):
    for item in items:
        path = Path(item["path"])
        if item.get("uploaded") and inside(path, upload_root) and path.parent != Path(upload_root).resolve():
            shutil.rmtree(path.parent, ignore_errors=True)


def prompt_note(items):
    if not items:
        return ""
    lines = [f"- {a['path']}（{'图片' if a['image'] else '文件'}，{a['size']} 字节）" for a in items]
    return "\n\n用户附带了以下本地文件，可以直接读取：\n" + "\n".join(lines)


PICK_FILES = r"""
import json, sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
paths = filedialog.askopenfilenames(parent=root, initialdir=sys.argv[1] or None, title="选择要附带的文件")
root.destroy()
print(json.dumps({"paths": list(paths)}))
"""


def pick_files(initial=""):
    start = str(initial) if initial and Path(initial).is_dir() else ""
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").is_file():
        python = python.with_name("python.exe")
    try:
        done = subprocess.run([str(python), "-c", PICK_FILES, start], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=600, creationflags=NO_WINDOW)
        paths = json.loads(done.stdout.decode("utf-8")).get("paths") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError("无法打开系统文件选择窗口") from None
    return [attachment(p) for p in paths[:10]]
