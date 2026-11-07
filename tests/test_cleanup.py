from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_workbench import files
from agent_workbench.adapters import Codex, Harness
from agent_workbench.engine import Engine
from agent_workbench.store import Store
from test_workbench import Fake, model_snapshot, result


class Messy(Fake):
    """An agent that delivers 4 real files and leaves verification junk behind, like the real run did."""
    def __init__(self, cwd):
        super().__init__([result("done")])
        self.cwd = Path(cwd)

    def run(self, *args, **kwargs):
        for name in ("index.html", "styles.css", "script.js", "naidragon.svg", "_shot-art.png", "_verify.mjs"):
            (self.cwd / name).write_text("x", encoding="utf-8")
        for n in range(30):
            (self.cwd / ".edge-profile" / "Default").mkdir(parents=True, exist_ok=True)
            (self.cwd / ".edge-profile" / "Default" / f"cache_{n}").write_text("x", encoding="utf-8")
        return super().run(*args, **kwargs)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.proj = self.base / "proj"
        self.proj.mkdir()
        (self.proj / "_existing.txt").write_text("mine", encoding="utf-8")   # looks temporary but predates the task
        (self.proj / ".gitignore").write_text("x", encoding="utf-8")
        (self.base / "root").mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_classification(self):
        self.assertTrue(files.is_temp(".edge-profile/Default/x"))
        self.assertTrue(files.is_temp("_shot.png"))
        self.assertFalse(files.is_temp("__init__.py"))
        self.assertFalse(files.is_temp("pkg/__init__.py"))
        self.assertFalse(files.is_temp("src/_private.py"))
        self.assertFalse(files.is_temp(".gitignore"))
        self.assertFalse(files.is_temp(".github/workflows/ci.yml"))

    def test_junk_is_listed_separately_and_cleaned_without_touching_older_files(self):
        harness = Messy(self.proj)
        engine = Engine(self.store, adapters={"codex": Fake([]), "harness": harness}, study_root=self.base / "root")
        task = self.store.create("做页面", str(self.proj), mode="harness")
        engine.run_task(task["id"])
        listed = files.list_files(self.store.get(task["id"]))
        real = sorted(f["name"] for f in listed if not f["temporary"])
        self.assertEqual(real, ["index.html", "naidragon.svg", "script.js", "styles.css"])
        folder = next(f for f in listed if f.get("folder"))
        self.assertEqual((folder["name"], folder["count"], folder["temporary"]), (".edge-profile", 30, True))
        self.assertEqual(engine.clean(task["id"]), {"removed": 32})
        self.assertFalse((self.proj / ".edge-profile").exists())
        self.assertFalse((self.proj / "_shot-art.png").exists())
        for kept in ("_existing.txt", ".gitignore", "index.html", "naidragon.svg"):
            self.assertTrue((self.proj / kept).exists(), kept)
        self.assertEqual(sorted(a["path"] for a in self.store.get(task["id"])["artifacts"]),
                         ["index.html", "naidragon.svg", "script.js", "styles.css"])

    def test_scratch_dir_is_offered_for_writing_rounds_only_and_removed_on_delete(self):
        harness = Fake([result()])
        engine = Engine(self.store, adapters={"codex": Fake([]), "harness": harness}, study_root=self.base / "root")
        task = self.store.create("x", str(self.proj), mode="harness")
        engine.run_task(task["id"])
        scratch = self.base / "data" / "scratch" / task["id"]
        self.assertEqual(harness.calls[0]["scratch"], scratch.resolve())
        self.assertIn(str(scratch.resolve()), harness.calls[0]["prompt"])
        engine.delete(task["id"])
        self.assertFalse(scratch.exists())

    def test_cli_commands_grant_the_scratch_dir(self):
        seen = []
        with patch("agent_workbench.adapters.run_process", lambda command, *a, **k: seen.append(command) or {}), \
                patch("agent_workbench.adapters.codex_program", return_value=["codex"]):
            Codex().run("x", "c", 1, threading.Event(), None, scratch="S")
            Codex().run("x", "c", 1, threading.Event(), None, scratch="S", readonly=True)
            Harness(self.base / "p").run("x", "c", 1, threading.Event(), None, model_config=model_snapshot(), scratch="S")
        self.assertIn("--add-dir=S", seen[0])
        self.assertNotIn("--add-dir=S", seen[1])
        self.assertEqual(seen[2][seen[2].index("--add-dir") + 1], "S")

    def test_clean_refuses_while_running(self):
        engine = Engine(self.store, adapters={"codex": Fake([]), "harness": Fake([])}, study_root=self.base / "root")
        task = self.store.create("x", str(self.proj), mode="harness")
        with self.assertRaisesRegex(ValueError, "运行"):
            engine.clean(task["id"])


if __name__ == "__main__":
    unittest.main()
