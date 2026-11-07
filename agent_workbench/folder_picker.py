from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

# Runs in a child process so a Tk window never lives inside the HTTP server's threads.
DIALOG = r"""
import json, sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1] or None, mustexist=True, title="选择项目文件夹")
root.destroy()
print(json.dumps({"path": path or None}))
"""

_busy = threading.Lock()


def pick_folder(initial="", timeout=600):
    """Open the native folder dialog on this machine; return an absolute folder path or None if cancelled."""
    if not _busy.acquire(blocking=False):
        raise ValueError("已有一个选择文件夹窗口打开，请先完成或关闭它")
    try:
        start = str(initial) if initial and Path(initial).is_dir() else ""
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").is_file():
            python = python.with_name("python.exe")
        try:
            process = subprocess.run([str(python), "-c", DIALOG, start], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            value = json.loads(process.stdout.decode("utf-8")).get("path")
        except subprocess.TimeoutExpired:
            raise ValueError("选择文件夹超时") from None
        except (OSError, ValueError, AttributeError):
            raise ValueError("无法打开系统文件夹选择窗口") from None
        if not value:
            return None
        path = Path(value).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("所选项目不是文件夹")
        return str(path)
    finally:
        _busy.release()
