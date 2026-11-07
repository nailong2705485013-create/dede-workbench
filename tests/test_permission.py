from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_workbench.adapters import Codex, Harness, sandbox_mode
from agent_workbench.engine import Engine
from agent_workbench.store import Store
from test_workbench import Fake, PLAN, model_snapshot, result, review


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cwd = self.base / "proj"
        self.cwd.mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, codex, harness):
        return Engine(self.store, adapters={"codex": codex, "harness": harness}, study_root=self.base / "s")

    def test_preset_names(self):
        self.assertEqual(sandbox_mode(True, "full"), "read-only")
        self.assertEqual(sandbox_mode(False, "normal"), "workspace-write")
        self.assertEqual(sandbox_mode(False, "full"), "danger-full-access")
        with self.assertRaises(ValueError):
            sandbox_mode(False, "root")
        with self.assertRaises(ValueError):
            self.store.create("x", str(self.cwd), permission="root")

    def test_full_task_keeps_planner_and_reviewer_read_only(self):
        codex, harness = Fake([PLAN, review(passed=True)]), Fake([result()])
        task = self.store.create("x", str(self.cwd), permission="full")
        self.engine(codex, harness).run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "passed")
        self.assertEqual([r["permission"] for r in saved["runs"]], ["read-only", "full", "read-only"])
        self.assertEqual(harness.calls[0]["permission"], "full")
        self.assertTrue(all(c["readonly"] for c in codex.calls))

    def test_full_access_once_applies_to_one_execution_round_only(self):
        codex = Fake([PLAN, review(), review(passed=True)])
        harness = Fake([result("denied", ok=False), result(), result()])
        engine = self.engine(codex, harness)
        task = self.store.create("x", str(self.cwd))
        engine.run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "runtime_failed")
        engine.resume(task["id"], full_access_once=True)
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "passed")
        self.assertEqual([c["permission"] for c in harness.calls], ["normal", "full", "normal"])
        self.assertFalse(saved["escalate"])
        self.assertIn("full_access_round", [e["kind"] for e in self.store.events(task["id"])])

    def test_escalation_rules(self):
        engine = self.engine(Fake([]), Fake([]))
        full = self.store.create("x", str(self.cwd), permission="full")
        self.store.update(full["id"], state="runtime_failed")
        with self.assertRaisesRegex(ValueError, "一般权限"):
            engine.resume(full["id"], full_access_once=True)
        chat = self.store.create_study("hi", "harness", self.cwd)
        self.store.update(chat["id"], state="runtime_failed")
        with self.assertRaisesRegex(ValueError, "一般权限"):
            engine.resume(chat["id"], full_access_once=True)

    def test_acl_preflight_only_applies_inside_the_sandbox(self):
        with patch("agent_workbench.engine.harness_can_grant", return_value=False):
            task = self.store.create("x", str(self.cwd), mode="harness", permission="full")
            self.engine(Fake([]), Fake([result()])).run_task(task["id"])
            self.assertEqual(self.store.get(task["id"])["state"], "passed")
            task = self.store.create("x", str(self.cwd), mode="harness")
            engine = self.engine(Fake([]), Fake([result()]))
            engine.run_task(task["id"])
            self.assertEqual(self.store.get(task["id"])["state"], "runtime_failed")
            engine.resume(task["id"], full_access_once=True)
            engine.pending.get_nowait()
            engine.run_task(task["id"])
            self.assertEqual(self.store.get(task["id"])["state"], "passed")

    def test_cli_commands_carry_the_preset(self):
        seen = []
        def run(command, *args, **kwargs):
            seen.append(command)
            return dict(ok=True, transport_ok=True, tool_errors=[], errors=[])
        with patch("agent_workbench.adapters.run_process", run), patch("agent_workbench.adapters.codex_program", return_value=["codex"]):
            Codex().run("x", str(self.cwd), 10, threading.Event(), lambda *_: None, permission="full")
            Harness(self.base / "p").run("x", str(self.cwd), 10, threading.Event(), lambda *_: None,
                                         model_config=model_snapshot(), permission="full")
            Harness(self.base / "p").run("x", str(self.cwd), 10, threading.Event(), lambda *_: None,
                                         model_config=model_snapshot(), readonly=True, permission="full")
        self.assertEqual(seen[0][seen[0].index("--sandbox") + 1], "danger-full-access")
        self.assertEqual(seen[1][seen[1].index("--permission-mode") + 1], "danger-full-access")
        self.assertEqual(seen[2][seen[2].index("--permission-mode") + 1], "read-only")


if __name__ == "__main__":
    unittest.main()
