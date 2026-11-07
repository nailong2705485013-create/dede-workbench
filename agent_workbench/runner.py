from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time


def redact(text):
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", str(text))
    return re.sub(r'''(?i)((?:access_token|refresh_token|api_key|authorization|password)["'\s:=]+)[^\s,"'}]+''',
                  r"\1[REDACTED]", text)


class WindowsJob:
    """Close this handle to terminate only this run's descendants, including on host exit."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w
        class IO(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]
        class BASIC(c.Structure):
            _fields_ = [("per_process", c.c_longlong), ("per_job", c.c_longlong), ("flags", w.DWORD),
                        ("minimum", c.c_size_t), ("maximum", c.c_size_t), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class EXTENDED(c.Structure):
            _fields_ = [("basic", BASIC), ("io", IO), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(info), c.sizeof(info)):
            if handle:
                kernel.CloseHandle(handle)
            raise OSError(c.get_last_error(), "无法建立子进程生命周期隔离")
        if not kernel.AssignProcessToJobObject(handle, w.HANDLE(process._handle)):
            error = c.get_last_error()
            kernel.CloseHandle(handle)
            raise OSError(error, "无法绑定子进程生命周期隔离")
        self.handle, self.kernel = handle, kernel

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def stop(process):
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


COMPLETION_GRACE = 1.5


def run_process(command, cwd, prompt, timeout, cancel, on_event, parser, env=None, process_hook=None):
    started = time.monotonic()
    result = dict(ok=False, exit_code=None, final=None, session_id=None, tool_errors=[],
                  errors=[], reason=None, timed_out=False, cancelled=False, elapsed_s=0)
    process, job = None, None
    items = queue.Queue(maxsize=2048)
    readers = []
    shutdown = threading.Event()
    tail = deque(maxlen=15)

    def put(item):
        while not shutdown.is_set():
            try:
                items.put(item, timeout=.2)
                return
            except queue.Full:
                pass

    def drain(name, pipe):
        try:
            for line in iter(pipe.readline, b""):
                put((name, line.decode("utf-8", errors="replace").rstrip()))
        finally:
            pipe.close()
            put((name, None))

    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
        try:
            job = WindowsJob(process)
        except OSError:
            stop(process)
            raise
        if process_hook:
            process_hook(process.pid)
        on_event("process_started", {"pid": process.pid, "program": Path(command[0]).name})
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            thread = threading.Thread(target=drain, args=(name, pipe), daemon=True)
            readers.append(thread)
            thread.start()

        def feed():
            try:
                process.stdin.write(prompt.encode("utf-8"))
                process.stdin.close()
            except (OSError, ValueError):
                pass
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        done = set()
        stopping_at = None
        while len(done) < 2:
            elapsed = time.monotonic() - started
            if stopping_at is None and (cancel.is_set() or elapsed >= timeout):
                result["cancelled"] = cancel.is_set()
                result["timed_out"] = not result["cancelled"]
                stopping_at = time.monotonic()
                if job:
                    job.close()
                stop(process)
            # A parser may mark the turn finished (Codex keeps running ~11 s of shutdown after its final event);
            # its session is already saved, so close the process tree after a short grace period.
            finished = result.get("finished_at")
            if stopping_at is None and finished and time.monotonic() - finished >= COMPLETION_GRACE:
                result["closed_after_completion"] = True
                stopping_at = time.monotonic()
                if job:
                    job.close()
                stop(process)
            if stopping_at and time.monotonic() - stopping_at > 5:
                result["errors"].append("停止后输出管道未关闭")
                break
            try:
                name, line = items.get(timeout=.1)
            except queue.Empty:
                continue
            if line is None:
                done.add(name)
            elif name == "stderr":
                if line:
                    tail.append(redact(line)[:1500])
            elif line.strip():
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("JSON event is not an object")
                    parser(event, result, on_event)
                except (ValueError, TypeError, KeyError) as exc:
                    # Never store the unparsed line: it might be private reasoning or credentials.
                    result["errors"].append("事件协议错误: " + type(exc).__name__)
        remaining = max(.1, timeout - (time.monotonic() - started))
        try:
            result["exit_code"] = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            if job:
                job.close()
            stop(process)
            result["exit_code"] = process.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        result["errors"].append(redact(str(exc)))
    finally:
        if job:
            job.close()
        if process and process.poll() is None:
            stop(process)
        shutdown.set()
        for reader in readers:
            reader.join(timeout=1)
        result["stderr_tail"] = list(tail)
        result["elapsed_s"] = round(time.monotonic() - started, 3)
    result["transport_ok"] = ((result["exit_code"] == 0 or result.get("closed_after_completion", False))
                              and result["reason"] == "completed" and
                              not result["errors"] and not result["timed_out"] and not result["cancelled"])
    result["ok"] = result["transport_ok"] and not result["tool_errors"]
    result.pop("finished_at", None)
    return result
