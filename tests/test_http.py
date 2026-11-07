from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from agent_workbench.engine import Engine
from agent_workbench.server import handler, LocalHTTPServer
from agent_workbench.store import Store
from test_workbench import model_snapshot
from unittest.mock import patch
from urllib.parse import quote


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = Engine(Store(self.root / "data"), self.root / "patch")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.engine, "test-token"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, extra=None, token=True):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Workbench-Token"] = "test-token"
        headers.update(extra or {})
        connection.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    def test_page_and_authenticated_task_creation(self):
        status, page = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"test-token", page)
        self.assertIn("dede工作台".encode(), page)
        status, data = self.request("/api/tasks", {"cwd": str(self.root), "prompt": "test", "mode": "harness"})
        self.assertEqual(status, 200)
        task = json.loads(data)
        self.assertEqual(task["state"], "queued")
        status, data = self.request("/api/tasks/" + task["id"] + "/summary")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["prompt"], "test")

    def test_page_keeps_launcher_token_pattern_and_local_resources(self):
        status, page = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"const token='test-token'", page)
        self.assertNotRegex(page, rb"""(?:src|href)=["']?(?:https?:)?//""")
        self.assertNotIn(b"__TOKEN__", page)

    def test_favicon_is_available_without_token(self):
        status, data = self.request("/favicon.ico", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\x00\x00\x01\x00"))

    def test_cross_origin_and_missing_token_rejected(self):
        self.assertEqual(self.request("/api/tasks", token=False)[0], 403)
        self.assertEqual(self.request("/api/tasks", {}, {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/", extra={"Host": "example.com"})[0], 403)

    def test_folder_pick_requires_token_and_returns_dialog_result(self):
        with patch("agent_workbench.server.pick_folder", return_value=str(self.root)) as picker, \
                patch("agent_workbench.server.harness_can_grant", return_value=True):
            self.assertEqual(self.request("/api/folders/pick", {}, token=False)[0], 403)
            picker.assert_not_called()
            status, data = self.request("/api/folders/pick", {"initial": "C:\\x"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data), {"path": str(self.root), "cancelled": False, "harness_ok": True})
            picker.assert_called_once_with("C:\\x")
        with patch("agent_workbench.server.pick_folder", return_value=None):
            self.assertEqual(json.loads(self.request("/api/folders/pick", {})[1]), {"path": None, "cancelled": True, "harness_ok": None})
        with patch("agent_workbench.server.pick_folder", side_effect=ValueError("busy")):
            self.assertEqual(self.request("/api/folders/pick", {})[0], 400)

    def test_folder_check_reports_harness_permission(self):
        with patch("agent_workbench.server.harness_can_grant", return_value=False):
            status, data = self.request("/api/folders/check?path=" + quote(str(self.root)))
        self.assertEqual((status, json.loads(data)), (200, {"exists": True, "harness_ok": False, "suggested_checks": []}))
        status, data = self.request("/api/folders/check?path=" + quote(str(self.root / "missing")))
        self.assertEqual(json.loads(data), {"exists": False, "harness_ok": None})
        self.assertEqual(json.loads(self.request("/api/folders/check?path=relative")[1])["exists"], False)
        self.assertEqual(self.request("/api/folders/check?path=x", token=False)[0], 403)

    def test_folder_picker_parses_child_output(self):
        from agent_workbench import folder_picker
        done = lambda out: type("P", (), {"stdout": out})()
        with patch("agent_workbench.folder_picker.subprocess.run", return_value=done(json.dumps({"path": str(self.root)}).encode())):
            self.assertEqual(folder_picker.pick_folder(), str(self.root.resolve()))
        with patch("agent_workbench.folder_picker.subprocess.run", return_value=done(b'{"path": null}')):
            self.assertIsNone(folder_picker.pick_folder())
        with patch("agent_workbench.folder_picker.subprocess.run", return_value=done(b"")):
            with self.assertRaises(ValueError):
                folder_picker.pick_folder()
        with folder_picker._busy:
            with self.assertRaises(ValueError):
                folder_picker.pick_folder()

    def test_study_routes(self):
        self.engine.study_root = self.root / "study-root"
        status, data = self.request("/api/study/root")
        self.assertEqual((status, json.loads(data)["ready"]), (200, False))
        self.assertEqual(self.request("/api/study", {"text": "hi", "executor": "harness"})[0], 400)
        status, data = self.request("/api/study/root/setup", {})
        self.assertEqual((status, json.loads(data)["ready"]), (200, True))
        status, data = self.request("/api/study", {"text": "hi", "executor": "codex"})
        task = json.loads(data)
        self.assertEqual((status, task["kind"], task["executor"]), (200, "study", "codex"))
        listed = json.loads(self.request("/api/tasks")[1])[0]
        self.assertEqual((listed["kind"], listed["executor"]), ("study", "codex"))
        status, data = self.request("/api/tasks/" + task["id"] + "/messages", {"text": "again", "executor": "codex", "interrupt": True})
        self.assertEqual((status, len(json.loads(data)["messages"])), (200, 2))
        self.assertEqual(self.request("/api/tasks/" + task["id"] + "/delete", {})[0], 400)
        self.engine.cancel(task["id"])
        status, data = self.request("/api/tasks/" + task["id"] + "/delete", {})
        self.assertEqual((status, json.loads(data)), (200, {"deleted": task["id"]}))
        self.assertFalse(Path(task["cwd"]).exists())
        self.assertEqual(self.request("/api/tasks/" + task["id"])[0], 404)
        self.assertEqual(self.request("/api/study/root/setup", {}, token=False)[0], 403)
        self.assertEqual(self.request("/api/models/codex")[0], 200)

    def test_empty_cwd_rejected(self):
        self.assertEqual(self.request("/api/tasks", {"cwd": "", "prompt": "test"})[0], 400)

    def test_cancel_queued_task(self):
        _, data = self.request("/api/tasks", {"cwd": str(self.root), "prompt": "test"})
        task_id = json.loads(data)["id"]
        status, data = self.request(f"/api/tasks/{task_id}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["state"], "cancelled")

    def test_external_request_does_not_render_prompt_as_html(self):
        _, data = self.request("/api/tasks", {"cwd": str(self.root), "prompt": "<script>alert(1)</script>"})
        self.assertEqual(json.loads(data)["prompt"], "<script>alert(1)</script>")
        # The dynamic page renderer applies esc() and textContent to all task strings.
        _, page = self.request("/")
        self.assertNotIn(b"<script>alert(1)</script>", page)

    def test_port_is_exclusive(self):
        with LocalHTTPServer(("127.0.0.1", 0), handler(self.engine, "token")) as first:
            with self.assertRaises(OSError):
                with LocalHTTPServer(("127.0.0.1", first.server_port), handler(self.engine, "token")):
                    pass

    def test_model_preview_and_task_api_hide_overlay(self):
        snapshot = model_snapshot()
        with patch.object(self.engine.adapters["harness"], "capture_model", return_value=snapshot):
            status, data = self.request("/api/models/harness")
        self.assertEqual(status, 200)
        self.assertNotIn("patch", json.loads(data))
        self.assertEqual(self.request("/api/models/harness", token=False)[0], 403)
        task = self.engine.submit(cwd=str(self.root), prompt="model test")
        self.engine.store.update(task["id"], harness_model=snapshot)
        for path in ("/api/tasks/" + task["id"], "/api/tasks/" + task["id"] + "/summary"):
            status, data = self.request(path)
            self.assertEqual(status, 200)
            self.assertNotIn("patch", json.loads(data)["harness_model"])

    def test_model_preview_failure_is_explicit(self):
        with patch.object(self.engine.adapters["harness"], "capture_model", side_effect=ValueError("invalid config")):
            status, data = self.request("/api/models/harness")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(data)["error"], "invalid config")

    def test_health_supports_default_desktop_policy(self):
        self.engine.adapters["harness"].patch = None
        status, data = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["model_policy"], "follow_desktop")


if __name__ == "__main__":
    unittest.main()
