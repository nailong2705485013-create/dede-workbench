from __future__ import annotations

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from agent_workbench import files
from agent_workbench.adapters import Codex
from agent_workbench.engine import Engine
from agent_workbench.server import handler
from agent_workbench.store import Store
from test_workbench import Fake, result

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.proj = self.base / "proj"
        (self.proj / "out").mkdir(parents=True)
        (self.proj / "out" / "chart.png").write_bytes(PNG)
        (self.proj / "run.bat").write_text("echo hi", encoding="utf-8")
        (self.base / "secret.txt").write_text("x", encoding="utf-8")
        (self.base / "root").mkdir()
        self.store = Store(self.base / "data")
        self.engine = Engine(self.store, adapters={"codex": Fake([]), "harness": Fake([])}, study_root=self.base / "root")
        self.task = self.store.create("x", str(self.proj), mode="harness")
        self.store.update(self.task["id"], artifacts=[{"path": "out\\chart.png", "change": "created"},
                                                      {"path": "run.bat", "change": "created"},
                                                      {"path": "gone.txt", "change": "deleted"}])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.engine, "t"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, body=None, raw=None, headers=None, token=True):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        h = {"Host": f"127.0.0.1:{self.server.server_port}", **({"X-Workbench-Token": "t"} if token else {}), **(headers or {})}
        if raw is not None:
            conn.request("POST", path, raw, {**h, "Content-Type": "application/octet-stream"})
        elif body is not None:
            conn.request("POST", path, json.dumps(body), {**h, "Content-Type": "application/json"})
        else:
            conn.request("GET", path, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data, r

    def test_list_marks_images_and_executables(self):
        status, data, _ = self.request(f"/api/tasks/{self.task['id']}/files")
        listed = {f["rel"]: f for f in json.loads(data)}
        self.assertEqual(status, 200)
        self.assertTrue(listed["out\\chart.png"]["image"])
        self.assertTrue(listed["run.bat"]["executable"])
        self.assertEqual(listed["gone.txt"]["change"], "deleted")

    def test_preview_only_images_inside_the_task(self):
        status, data, r = self.request(f"/api/tasks/{self.task['id']}/preview?path=" + quote("out\\chart.png"))
        self.assertEqual((status, data, r.getheader("Content-Type")), (200, PNG, "image/png"))
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.request(f"/api/tasks/{self.task['id']}/preview?path=" + quote(str(self.base / "secret.txt")))[0], 400)
        self.assertEqual(self.request(f"/api/tasks/{self.task['id']}/preview?path=" + quote("..\\secret.txt"))[0], 400)
        self.assertEqual(self.request(f"/api/tasks/{self.task['id']}/preview?path=run.bat")[0], 400)
        self.assertEqual(self.request(f"/api/tasks/{self.task['id']}/preview?path=x", token=False)[0], 403)

    def test_open_actions_and_executable_guard(self):
        calls = []
        with patch("agent_workbench.files._start", lambda p: calls.append(("start", str(p)))), \
                patch("agent_workbench.files.subprocess.Popen", lambda argv, **k: calls.append(tuple(argv[:2]))):
            ok = lambda body: self.request(f"/api/tasks/{self.task['id']}/open", body)
            self.assertEqual(json.loads(ok({"path": "out\\chart.png", "action": "open"})[1])["done"], "open")
            blocked = json.loads(ok({"path": "run.bat", "action": "open"})[1])
            self.assertEqual((blocked["done"], blocked["blocked"]), ("reveal", True))
            self.assertEqual(json.loads(ok({"path": "run.bat", "action": "open", "run": True})[1])["done"], "open")
            self.assertEqual(json.loads(ok({"path": "out\\chart.png", "action": "with"})[1])["done"], "with")
            self.assertEqual(json.loads(ok({"action": "folder"})[1])["done"], "folder")
            self.assertEqual(ok({"path": str(self.base / "secret.txt"), "action": "open"})[0], 400)
            self.assertEqual(ok({"path": "out\\chart.png", "action": "delete"})[0], 400)
        self.assertEqual(calls[0][0], "start")
        self.assertEqual(calls[1], ("explorer", "/select,"))
        self.assertEqual(calls[3], ("rundll32.exe", "shell32.dll,OpenAs_RunDLL"))
        self.assertEqual(len([c for c in calls if c[0] == "start"]), 3)

    def test_upload_then_attach_and_preview_and_delete(self):
        status, data, _ = self.request("/api/attachments", raw=PNG, headers={"X-File-Name": quote("截图 1.png")})
        up = json.loads(data)
        self.assertEqual((status, up["name"], up["image"], up["uploaded"]), (200, "截图 1.png", True, True))
        self.assertTrue(Path(up["path"]).is_relative_to((self.base / "data" / "attachments").resolve()))
        self.assertEqual(self.request("/api/attachments", raw=PNG, token=False)[0], 403)
        self.assertEqual(self.request("/api/attachments", raw=b"")[0], 400)
        self.engine.setup_study_root = lambda: None
        with patch.object(Engine, "study_ready", return_value=True):
            status, data, _ = self.request("/api/study", {"text": "看看这张图", "executor": "codex", "attachments": [up]})
        chat = json.loads(data)
        self.assertEqual(chat["messages"][0]["attachments"][0]["path"], up["path"])
        status, data, _ = self.request(f"/api/tasks/{chat['id']}/preview?path=" + quote(up["path"]))
        self.assertEqual((status, data), (200, PNG))
        self.engine.cancel(chat["id"])
        self.request(f"/api/tasks/{chat['id']}/delete", {})
        self.assertFalse(Path(up["path"]).exists())

    def test_bogus_attachment_paths_are_rejected(self):
        self.assertEqual(self.request("/api/tasks", {"prompt": "x", "cwd": str(self.proj), "attachments": [{"path": str(self.base / "nope.png")}]})[0], 400)
        self.assertEqual(self.request("/api/tasks", {"prompt": "x", "cwd": str(self.proj), "attachments": "x"})[0], 400)


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.pic = self.base / "a.png"
        self.pic.write_bytes(PNG)
        self.proj = self.base / "proj"
        self.proj.mkdir()
        (self.base / "root").mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_command_attaches_images_new_and_resumed(self):
        seen = []
        with patch("agent_workbench.adapters.run_process", lambda command, *a, **k: seen.append(command) or {}), \
                patch("agent_workbench.adapters.codex_program", return_value=["codex"]):
            Codex().run("x", "c", 1, threading.Event(), None, images=[str(self.pic)])
            Codex().run("x", "c", 1, threading.Event(), None, session_id="s1", images=[str(self.pic)])
        new, resumed = seen
        self.assertEqual(new[-1], "-")
        self.assertIn(f"--image={self.pic}", new)
        self.assertLess(new.index(f"--image={self.pic}"), new.index("--color"))
        self.assertEqual(resumed[resumed.index("resume"):], ["resume", "s1", f"--image={self.pic}", "-"])

    def test_study_sends_paths_in_text_and_pictures_to_codex_only(self):
        codex, harness = Fake([result("看到了")]), Fake([result("我读取了路径")])
        engine = Engine(self.store, adapters={"codex": codex, "harness": harness}, study_root=self.base / "root")
        att = files.attachment(self.pic)
        task = engine.submit_study("这是什么", "codex", [att])
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        self.assertEqual(codex.calls[0]["images"], [str(self.pic.resolve())])
        self.assertIn(str(self.pic.resolve()), codex.calls[0]["prompt"])
        engine.add_message(task["id"], "换你看看", "harness")
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        self.assertNotIn("images", harness.calls[0])
        self.assertIn(str(self.pic.resolve()), harness.calls[0]["prompt"])  # handed over as a path

    def test_work_pictures_go_to_each_new_codex_session_once(self):
        from test_workbench import PLAN, review
        codex = Fake([PLAN, review(passed=True)])
        engine = Engine(self.store, adapters={"codex": codex, "harness": Fake([result()])}, study_root=self.base / "root")
        task = engine.submit(prompt="照着图做", cwd=str(self.proj), attachments=[files.attachment(self.pic)])
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        self.assertEqual([c["images"] for c in codex.calls], [[str(self.pic.resolve())]] * 2)
        self.assertIn("用户附带了以下本地文件", codex.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
