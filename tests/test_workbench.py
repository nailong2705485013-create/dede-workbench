from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_workbench.engine import Engine
from agent_workbench.runner import run_process
from agent_workbench.store import Store
from agent_workbench.server import InstanceLock


def model_snapshot(provider="test-route", model="test-model"):
    patch = [{"id": "agent-default-model", "config": {"provider": provider, "model": model}}]
    fingerprint = hashlib.sha256(json.dumps(patch, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return dict(policy="follow_desktop", provider=provider, model=model, reasoning_effort=None,
                fingerprint=fingerprint, patch=patch, source="fixture", captured_at="fixture")


def result(text="done", ok=True, tools=None):
    return dict(ok=ok and not tools, transport_ok=ok, final=text, session_id="s-test", tool_errors=tools or [],
                errors=[] if ok else ["network failed"], exit_code=0 if ok else 1, reason="completed")


class Fake:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def capture_model(self):
        return model_snapshot()

    def run(self, prompt, cwd, timeout, cancel, on_event, **kwargs):
        self.calls.append(dict(prompt=prompt, cwd=cwd, **kwargs))
        value = self.outcomes.pop(0)
        on_event("session", {"session_id": value["session_id"]})
        return value


PLAN = result(json.dumps(dict(plan="Implement", steps=["write"], acceptance=["inspect"])))


def review(passed=False, minor=False):
    return result(json.dumps(dict(passed=passed, summary="checked", issues=[] if passed else ["fix"], minor_patch=minor)))


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "中文 空间"
        self.cwd.mkdir()
        self.store = Store(self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, codex, harness):
        return Engine(self.store, self.root / "patch", {"codex": codex, "harness": harness})

    def task(self, **kwargs):
        return self.store.create("test task", str(self.cwd), **kwargs)

    def test_initial_plus_exactly_three_repairs_then_stop(self):
        codex, harness = Fake([PLAN] + [review()] * 4), Fake([result()] * 4)
        task = self.task()
        self.engine(codex, harness).run_task(task["id"])
        final = self.store.get(task["id"])
        self.assertEqual((final["state"], final["repair_count"]), ("needs_user", 3))
        self.assertEqual(len(harness.calls), 4)
        self.assertFalse(final["takeover"])
        self.assertEqual(final["runtime_failures"], 0)

    def test_runtime_failure_does_not_spend_another_repair(self):
        codex, harness = Fake([PLAN, review(), review(True)]), Fake([result(), result(ok=False), result()])
        task = self.task()
        engine = self.engine(codex, harness)
        engine.run_task(task["id"])
        failed = self.store.get(task["id"])
        self.assertEqual((failed["state"], failed["repair_count"], failed["runtime_failures"]), ("runtime_failed", 1, 1))
        engine.resume(task["id"])
        engine.run_task(task["id"])
        final = self.store.get(task["id"])
        self.assertEqual((final["state"], final["repair_count"]), ("passed", 1))
        self.assertEqual(harness.calls[-1]["session_id"], "s-test")
        self.assertEqual(harness.calls[-1]["cwd"], str(self.cwd.resolve()))

    def test_codex_can_patch_minor_issue(self):
        codex, harness = Fake([PLAN, review(minor=True), result("patched"), review(True)]), Fake([result()])
        task = self.task()
        self.engine(codex, harness).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "passed")
        self.assertEqual(len(harness.calls), 1)
        self.assertFalse(codex.calls[2]["readonly"])

    def test_completed_with_tool_failure_never_passes_single_mode(self):
        task = self.task(mode="harness")
        self.engine(Fake([]), Fake([result(tools=["tool failed"])])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "needs_user")

    def test_sandbox_tool_failure_is_runtime_not_quality(self):
        task = self.task(mode="harness")
        self.engine(Fake([]), Fake([result(tools=["SetNamedSecurityInfoW failed (Win32 5)"])])).run_task(task["id"])
        final = self.store.get(task["id"])
        self.assertEqual((final["state"], final["repair_count"]), ("runtime_failed", 0))

    def test_recovery_requires_explicit_resume(self):
        task = self.task()
        self.store.update(task["id"], state="executing", sessions={"harness:executor": "saved"})
        Store(self.store.root).recover()
        recovered = self.store.get(task["id"])
        self.assertEqual(recovered["state"], "interrupted")
        self.assertEqual(recovered["sessions"]["harness:executor"], "saved")

    def test_full_takeover_requires_explicit_action(self):
        task = self.task()
        self.store.update(task["id"], state="needs_user", repair_count=3)
        engine = self.engine(Fake([result()]), Fake([]))
        with self.assertRaises(ValueError):
            engine.resume(task["id"])
        engine.resume(task["id"], takeover=True)
        engine.run_task(task["id"])
        final = self.store.get(task["id"])
        self.assertTrue(final["takeover"])
        self.assertEqual(final["state"], "passed")

    def test_timeout_and_cancel_stop_process(self):
        for cancel_immediately in (False, True):
            cancel = threading.Event()
            if cancel_immediately:
                cancel.set()
            started = time.monotonic()
            value = run_process([sys.executable, "-c", "import time;time.sleep(20)"], self.cwd, "", .3,
                                cancel, lambda *_: None, lambda *_: None)
            self.assertFalse(value["ok"])
            self.assertTrue(value["cancelled"] if cancel_immediately else value["timed_out"])
            self.assertLess(time.monotonic() - started, 8)

    def test_codex_process_is_closed_shortly_after_turn_completed(self):
        from agent_workbench.adapters import Codex
        child = ("import json,time;print(json.dumps({'type':'thread.started','thread_id':'t1'}),flush=True);"
                 "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'hi'}}),flush=True);"
                 "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True);time.sleep(30)")
        started = time.monotonic()
        with patch("agent_workbench.adapters.codex_program", return_value=[sys.executable, "-c", child]):
            value = Codex().run("x", str(self.cwd), 60, threading.Event(), lambda *_: None)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(value["ok"])
        self.assertTrue(value["closed_after_completion"])
        self.assertEqual((value["final"], value["session_id"]), ("hi", "t1"))
        self.assertNotIn("finished_at", value)

    def test_process_requires_final_even_with_zero_exit(self):
        value = run_process([sys.executable, "-c", "print('{}')"], self.cwd, "", 5,
                            threading.Event(), lambda *_: None, lambda *_: None)
        self.assertEqual(value["exit_code"], 0)
        self.assertFalse(value["ok"])

    def test_instance_lock_prevents_second_host(self):
        with InstanceLock(self.root / "instance.lock"):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.root / "instance.lock"):
                    pass

    def test_acceptance_failure_cannot_pass(self):
        task = self.task(mode="harness", checks=[[sys.executable, "-c", "raise SystemExit(1)"]])
        self.engine(Fake([]), Fake([result()])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "needs_user")

    def test_thinking_content_not_written_by_harness_adapter(self):
        # Real parser contract is exercised through a tiny JSONL fixture process.
        from agent_workbench.adapters import Harness
        from unittest.mock import patch
        events = []
        fixture = [dict(type="thinking", text="PRIVATE_THINKING"), dict(type="session", sessionId="s1"),
                   dict(type="final", sessionId="s1", reason=dict(kind="completed"), text="ok")]
        def run(command, cwd, prompt, timeout, cancel, emit, parser, env):
            out = dict(ok=True, transport_ok=True, tool_errors=[], errors=[])
            for event in fixture:
                parser(event, out, emit)
            return out
        with patch("agent_workbench.adapters.run_process", run):
            Harness(self.root / "p").run("task", self.cwd, 10, threading.Event(), lambda k,d: events.append((k,d)),
                                         model_config=model_snapshot())
        self.assertNotIn("PRIVATE_THINKING", json.dumps(events))

    def test_harness_folder_without_full_control_stops_before_any_model_call(self):
        from unittest.mock import patch
        from agent_workbench.workspace import HARNESS_ACL_HINT
        codex, harness = Fake([PLAN]), Fake([result()])
        for mode in ("combined", "harness"):
            task = self.task(mode=mode)
            with patch("agent_workbench.engine.harness_can_grant", return_value=False):
                self.engine(codex, harness).run_task(task["id"])
            saved = self.store.get(task["id"])
            self.assertEqual(saved["state"], "runtime_failed")
            self.assertEqual(saved["stop_reason"], HARNESS_ACL_HINT)
            self.assertIsNone(saved["harness_model"])
        self.assertEqual(codex.calls + harness.calls, [])
        task = self.task(mode="codex")
        with patch("agent_workbench.engine.harness_can_grant", return_value=False):
            self.engine(Fake([result()]), Fake([])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "passed")

    def test_two_hosts_cannot_modify_same_workspace(self):
        entered, release = threading.Event(), threading.Event()
        class Blocking(Fake):
            def run(self, *args, **kwargs):
                entered.set()
                release.wait(5)
                return result()
        first, second = self.task(mode="harness"), self.task(mode="harness")
        engine = self.engine(Fake([]), Blocking([]))
        thread = threading.Thread(target=engine.run_task, args=(first["id"],))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.engine(Fake([]), Fake([])).run_task(second["id"])
            self.assertEqual(self.store.get(second["id"])["state"], "runtime_failed")
        finally:
            release.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
