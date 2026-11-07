from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from agent_workbench.engine import Engine
from agent_workbench.store import Store
from agent_workbench.workspace import detect_checks, show_command
from test_workbench import Fake, PLAN, result, review


def verdict(passed=False, blocked=False, issues=None):
    return result(json.dumps(dict(passed=passed, summary="checked", issues=issues if issues is not None else ([] if passed else ["x"]),
                                  minor_patch=False, verification_blocked=blocked)))


PASSING_TEST = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING_TEST = "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail('boom')\n"


class VerificationTests(unittest.TestCase):
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

    def write_tests(self, body, package=True):
        (self.cwd / "tests").mkdir(exist_ok=True)
        if package:
            (self.cwd / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.cwd / "tests" / "test_x.py").write_text(body, encoding="utf-8")

    def test_detects_test_command_only_from_project_files(self):
        self.assertEqual(detect_checks(self.cwd), [])
        self.write_tests(PASSING_TEST)
        self.assertEqual(detect_checks(self.cwd), [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"]])
        self.assertEqual(show_command(detect_checks(self.cwd)[0]), "python -m unittest discover -s tests -t . -q")
        (self.cwd / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        self.assertEqual(detect_checks(self.cwd)[0][1:], ["-m", "pytest", "-q"])
        node = self.base / "node"
        node.mkdir()
        (node / "package.json").write_text('{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}', encoding="utf-8")
        self.assertEqual(detect_checks(node), [])
        (node / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
        self.assertEqual(detect_checks(node)[0][1:], ["test", "--silent"])

    def test_tests_written_during_the_run_are_found_and_given_to_the_reviewer(self):
        class Writes(Fake):
            def run(inner, *args, **kwargs):
                self.write_tests(PASSING_TEST)
                return super().run(*args, **kwargs)
        codex = Fake([PLAN, verdict(passed=True)])
        task = self.store.create("x", str(self.cwd), auto_checks=True)
        self.engine(codex, Writes([result()])).run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "passed")
        self.assertTrue(saved["last_checks"][0]["auto"] and saved["last_checks"][0]["passed"])
        self.assertIn("checks_run_by_workbench", codex.calls[1]["prompt"])

    def test_recovered_tool_error_does_not_force_a_failed_review(self):
        codex = Fake([PLAN, verdict(passed=True)])
        harness = Fake([result(tools=["Error: old_string was not found in ledger.py"])])
        task = self.store.create("x", str(self.cwd))
        self.engine(codex, harness).run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual((saved["state"], saved["repair_count"]), ("passed", 0))
        self.assertIn("old_string was not found", codex.calls[1]["prompt"])
        self.assertIn("intermediate_tool_errors", codex.calls[1]["prompt"])

    def test_failing_checks_still_override_the_reviewer(self):
        self.write_tests(FAILING_TEST)
        codex = Fake([PLAN] + [verdict(passed=True)] * 4)
        task = self.store.create("x", str(self.cwd), auto_checks=True)
        engine = self.engine(codex, Fake([result()] * 4))
        engine.run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertFalse(any(r["passed"] for r in saved["reviews"]))
        self.assertIn("验收命令未通过", saved["reviews"][0]["issues"][-1])
        self.assertEqual((saved["state"], saved["repair_count"]), ("needs_user", 3))

    def test_blocked_verification_pauses_without_spending_a_repair_and_can_recheck(self):
        codex = Fake([PLAN, verdict(blocked=True, issues=["测试需要写临时文件，只读环境无法运行"]), verdict(passed=True)])
        task = self.store.create("x", str(self.cwd))
        engine = self.engine(codex, Fake([result()]))
        engine.run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual((saved["state"], saved["stage"], saved["repair_count"]), ("needs_user", "review", 0))
        self.assertIn("无法独立验证", saved["stop_reason"])
        self.write_tests(PASSING_TEST)
        engine.resume(task["id"], auto_checks=True)
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual((saved["state"], saved["repair_count"]), ("passed", 0))
        self.assertTrue(saved["last_checks"][0]["passed"])
        self.assertEqual(len(codex.calls), 3)  # no extra execution round

    def test_old_style_review_without_the_new_field_is_still_accepted(self):
        codex = Fake([PLAN, review(passed=True)])
        task = self.store.create("x", str(self.cwd))
        self.engine(codex, Fake([result()])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "passed")

    def test_single_executor_uses_checks_when_present(self):
        self.write_tests(PASSING_TEST)
        task = self.store.create("x", str(self.cwd), mode="harness", auto_checks=True)
        self.engine(Fake([]), Fake([result(tools=["Error: edit failed once"])])).run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "passed")
        self.assertIn("验收命令通过", saved["stop_reason"])
        bare = self.base / "bare"
        bare.mkdir()
        task = self.store.create("x", str(bare), mode="harness")
        self.engine(Fake([]), Fake([result(tools=["Error: edit failed once"])])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "needs_user")


if __name__ == "__main__":
    unittest.main()
