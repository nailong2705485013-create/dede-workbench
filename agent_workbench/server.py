from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .adapters import codex_program
from .engine import Engine
from .folder_picker import pick_folder
from .workspace import detect_checks, harness_can_grant, show_command
from .study import codex_model
from . import files
from urllib.parse import unquote
from .locks import InstanceLock
from .store import Store
from .model_config import desktop_source, public_model
from . import __version__


def public_task(task):
    return {**task, "harness_model": public_model(task.get("harness_model"))}


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def handler(engine, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, value, content_type="application/json; charset=utf-8",
                    csp="default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'"):
            payload = value if isinstance(value, bytes) else (value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self):
            port = self.server.server_port
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.respond(403, {"error": "仅允许本机访问"})
                return False
            return True

        def do_GET(self):
            if not self.valid_host():
                return
            url = urlsplit(self.path)
            if url.path == "/":
                page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
                self.respond(200, page.replace("__TOKEN__", token), "text/html; charset=utf-8")
                return
            if url.path == "/favicon.ico":
                self.respond(200, Path(__file__).with_name("assets").joinpath("dede.ico").read_bytes(), "image/x-icon")
                return
            if self.headers.get("X-Workbench-Token") != token:
                self.respond(403, {"error": "访问令牌无效，请刷新工作台页面"})
                return
            try:
                if url.path == "/api/tasks":
                    tasks = engine.store.list()
                    keys = ("id", "prompt", "mode", "state", "stage", "cwd", "updated", "repair_count", "runtime_failures")
                    self.respond(200, [{**{k: t[k] for k in keys}, "kind": t.get("kind", "work"), "executor": t.get("executor")}
                                       for t in tasks])
                elif url.path == "/api/health":
                    try:
                        codex = codex_program()
                    except OSError as exc:
                        codex = [str(exc)]
                    harness = engine.adapters["harness"]
                    self.respond(200, {"codex": codex, "harness_installed": (harness.install / "DeepSeek Harness.exe").is_file(),
                                       "patch_exists": (harness.patch or desktop_source()).is_file(), "scheduler": engine.scheduler_state(),
                                       "model_policy": "explicit_patch" if harness.patch else "follow_desktop",
                                       "pid": os.getpid(), "data_dir": str(engine.store.root), "version": __version__})
                elif url.path == "/api/folders/check":
                    path = Path(parse_qs(url.query).get("path", [""])[0])
                    if not path.is_absolute() or not path.is_dir():
                        self.respond(200, {"exists": False, "harness_ok": None})
                    else:
                        try:
                            ok = harness_can_grant(path)
                        except OSError:
                            ok = None
                        self.respond(200, {"exists": True, "harness_ok": ok,
                                           "suggested_checks": [show_command(a) for a in detect_checks(path)]})
                elif url.path == "/api/study/root":
                    root = engine.study_root
                    try:
                        ready = engine.study_ready()
                    except OSError:
                        ready = False
                    self.respond(200, {"root": str(root), "exists": root.is_dir(), "ready": ready})
                elif url.path == "/api/models/codex":
                    self.respond(200, codex_model())
                elif url.path == "/api/models/harness":
                    try:
                        self.respond(200, public_model(engine.adapters["harness"].capture_model()))
                    except (OSError, ValueError) as exc:
                        self.respond(503, {"error": str(exc)})
                elif url.path.startswith("/api/tasks/"):
                    parts = url.path.strip("/").split("/")
                    task_id = parts[2]
                    if len(parts) == 4 and parts[3] == "events":
                        self.respond(200, engine.store.events(task_id, int(parse_qs(url.query).get("after", [0])[0])))
                    elif len(parts) == 4 and parts[3] == "files":
                        self.respond(200, files.list_files(engine.store.get(task_id)))
                    elif len(parts) == 4 and parts[3] == "preview":
                        data, kind = files.read_preview(engine.store.get(task_id), parse_qs(url.query).get("path", [""])[0],
                                                        engine.upload_root)
                        self.respond(200, data, kind, csp="default-src 'none'; style-src 'unsafe-inline'; sandbox")
                    elif len(parts) == 4 and parts[3] == "summary":
                        self.respond(200, engine.store.summary(task_id))
                    elif len(parts) == 3:
                        self.respond(200, public_task(engine.store.get(task_id)))
                    else:
                        self.respond(404, {"error": "未找到"})
                else:
                    self.respond(404, {"error": "未找到"})
            except KeyError:
                self.respond(404, {"error": "任务不存在"})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})

        def do_POST(self):
            if not self.valid_host():
                return
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if self.headers.get("X-Workbench-Token") != token or (origin and origin not in allowed):
                self.respond(403, {"error": "请求来源或令牌无效"})
                return
            if urlsplit(self.path).path == "/api/attachments":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= files.MAX_UPLOAD:
                        raise ValueError("附件为空或超过 20 MB")
                    name = unquote(self.headers.get("X-File-Name", "file"))
                    self.respond(200, files.save_upload(engine.upload_root, name, self.rfile.read(length)))
                except (ValueError, OSError) as exc:
                    self.respond(400, {"error": str(exc)})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.respond(415, {"error": "需要 JSON 请求"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128000:
                    raise ValueError("请求大小无效")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求必须是 JSON 对象")
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ["api", "shutdown"]:
                    self.respond(200, {"stopping": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                elif parts == ["api", "folders", "pick"]:
                    path = pick_folder(body.get("initial") or "")
                    self.respond(200, {"path": path, "cancelled": path is None,
                                       "harness_ok": harness_can_grant(path) if path else None})
                    return
                elif parts == ["api", "study", "root", "setup"]:
                    self.respond(200, {"root": str(engine.setup_study_root()), "ready": True})
                    return
                elif parts == ["api", "attachments", "pick"]:
                    self.respond(200, files.pick_files(body.get("initial") or ""))
                    return
                elif parts == ["api", "study"]:
                    value = engine.submit_study(body.get("text"), body.get("executor", "harness"), body.get("attachments"))
                elif parts == ["api", "tasks"]:
                    value = engine.submit(prompt=body.get("prompt"), cwd=body.get("cwd", ""), mode=body.get("mode", "combined"),
                                          timeout=body.get("timeout", 600), checks=body.get("checks", []),
                                          permission=body.get("permission", "normal"), auto_checks=body.get("auto_checks") is True,
                                          attachments=body.get("attachments"))
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"]:
                    if parts[3] == "cancel":
                        value = engine.cancel(parts[2])
                    elif parts[3] == "messages":
                        value = engine.add_message(parts[2], body.get("text"), body.get("executor"),
                                                   interrupt=body.get("interrupt") is True, attachments=body.get("attachments"))
                    elif parts[3] == "open":
                        self.respond(200, files.open_path(engine.store.get(parts[2]), body.get("path", ""), body.get("action"),
                                                          engine.upload_root, confirmed_run=body.get("run") is True))
                        return
                    elif parts[3] == "clean":
                        self.respond(200, engine.clean(parts[2]))
                        return
                    elif parts[3] == "delete":
                        self.respond(200, engine.delete(parts[2]))
                        return
                    elif parts[3] in {"resume", "takeover"}:
                        value = engine.resume(parts[2], takeover=parts[3] == "takeover",
                                              full_access_once=body.get("full_access_once") is True,
                                              auto_checks=body.get("auto_checks") is True)
                    else:
                        self.respond(404, {"error": "未找到"})
                        return
                else:
                    self.respond(404, {"error": "未找到"})
                    return
                self.respond(200, public_task(value))
            except KeyError:
                self.respond(404, {"error": "任务不存在"})
            except (ValueError, TypeError, OSError) as exc:
                self.respond(400, {"error": str(exc)})
    return Handler


def serve(data, patch=None, port=8765, open_browser=False):
    store = Store(data)
    with InstanceLock(store.root / "instance.lock"):
        engine = Engine(store, patch)
        token = secrets.token_urlsafe(32)
        server = LocalHTTPServer(("127.0.0.1", port), handler(engine, token))
        server.daemon_threads = True
        engine.start()
        url = f"http://127.0.0.1:{server.server_port}/"
        (store.root / "server.pid").write_text(str(os.getpid()), encoding="ascii")
        print(f"dede工作台：{url}\n数据：{store.root}\n按 Ctrl+C 停止。", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            engine.close()
