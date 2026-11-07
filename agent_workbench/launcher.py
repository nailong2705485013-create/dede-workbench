"""Desktop entry point: reuse the local service or start it without a console."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler
import webbrowser

from .locks import InstanceLock


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / ".agent-workbench"
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"


def probe(data=DATA, url=URL):
    """None means no listener; another or unresponsive service is an error."""
    opener = build_opener(ProxyHandler({}))
    try:
        # Windows can take about two seconds to report a refused loopback connection.
        with opener.open(url, timeout=4) as response:
            page = response.read(256000).decode("utf-8")
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return None
        raise RuntimeError("本地端口没有正常响应，请稍后重试。") from None
    except (OSError, UnicodeError):
        raise RuntimeError("本地端口没有正常响应，请稍后重试。") from None
    token = re.search(r"const token='([A-Za-z0-9_-]+)'", page)
    if not token:
        raise RuntimeError("8765 端口被其他程序占用，无法打开 dede工作台。")
    request = Request(url + "api/health", headers={"X-Workbench-Token": token[1]})
    try:
        with opener.open(request, timeout=2) as response:
            health = json.loads(response.read(64000))
        if (not isinstance(health, dict) or not isinstance(health.get("data_dir"), str)
                or Path(health["data_dir"]).resolve() != Path(data).resolve()):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise RuntimeError("8765 端口上的服务不是此目录的 dede工作台，请先处理端口占用。") from None
    return health


def spawn_server(root=ROOT, data=DATA):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    command = [str(python), "-m", "agent_workbench", "--data", str(data), "serve", "--port", str(PORT)]
    with (data / "server.stdout.log").open("ab") as stdout, (data / "server.stderr.log").open("ab") as stderr:
        return subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def wait_ready(data, url, timeout, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = probe(data, url)
        if health:
            return health
        if process is not None and process.poll() is not None:
            raise RuntimeError("工作台启动失败，请查看数据目录中的 server.stderr.log。")
        time.sleep(.2)
    raise RuntimeError("工作台启动超时，请稍后重试或查看 server.stderr.log。")


def ensure_running(root=ROOT, data=DATA, url=URL, timeout=20):
    data = Path(data)
    lock = InstanceLock(data / "launcher.lock")
    try:
        lock.__enter__()
    except RuntimeError:
        return wait_ready(data, url, timeout)
    try:
        health = probe(data, url)
        if health:
            return health
        return wait_ready(data, url, timeout, spawn_server(root, data))
    finally:
        lock.__exit__(None, None, None)


def main():
    try:
        ensure_running()
        if not webbrowser.open(URL, new=2):
            raise RuntimeError("工作台已启动。请在浏览器打开 http://127.0.0.1:8765/ 。")
        return 0
    except (OSError, RuntimeError) as exc:
        DATA.mkdir(parents=True, exist_ok=True)
        with (DATA / "launcher.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {exc}\n")
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "dede工作台", 0x10)
        elif sys.stderr:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
