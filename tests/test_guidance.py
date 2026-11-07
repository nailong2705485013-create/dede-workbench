from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from agent_workbench.engine import Engine
from agent_workbench.store import Store
from test_workbench import Fake, PLAN, result, review


class During(Fake):
    """A fake agent whose Nth run is where the user types something."""
    def __init__(self, outcomes, on_run=None):
        super().__init__(outcomes)
        self.on_run = on_run or {}

    def run(self, prompt, cwd, timeout, cancel, on_event, **kwargs):
        action = self.on_run.get(len(self.calls))
        value = super().run(prompt, cwd, timeout, cancel, on_event, **kwargs)
        if action:
            action()
            if cancel.is_set():
                return {**value, "ok": False, "transport_ok": False, "cancelled": True, "final": None}
        return value


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cwd = self.base / "proj"
        self.cwd.mkdir()
        (self.base / "root").mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, codex, harness):
        return Engine(self.store, adapters={"codex": codex, "harness": harness}, study_root=self.base / "root")

    def run_like_worker(self, engine, task_id):
        cancel = threading.Event()
        with engine.flags_lock:
            engine.cancel_flags[task_id] = cancel
        try:
            engine.run_task(task_id, cancel)
        finally:
            with engine.flags_lock:
                engine.cancel_flags.pop(task_id, None)

    def test_queued_guidance_runs_after_the_round_and_does_not_count_as_repair(self):
        holder = {}
        harness = During([result("v1"), result("v2")], {0: lambda: holder["e"].guide(holder["id"], "改成用 SQLite")})
        engine = holder["e"] = self.engine(Fake([]), harness)
        task = holder["id"] = self.store.create("做个记账工具", str(self.cwd), mode="harness")["id"]
        self.run_like_worker(engine, task)
        saved = self.store.get(task)
        self.assertEqual((saved["state"], saved["repair_count"]), ("passed", 0))
        self.assertEqual(len(harness.calls), 2)
        self.assertIn("刚刚补充或纠正", harness.calls[1]["prompt"])
        self.assertIn("改成用 SQLite", harness.calls[1]["prompt"])
        self.assertTrue(saved["guidance"][0]["applied"])
        self.assertEqual([r["stage"] for r in saved["runs"]], ["execute", "guide"])

    def test_interrupt_stops_only_the_round_and_continues_with_guidance(self):
        holder = {}
        harness = During([result("v1"), result("v2")],
                         {0: lambda: holder["e"].guide(holder["id"], "方向错了，别用框架", interrupt=True)})
        engine = holder["e"] = self.engine(Fake([]), harness)
        task = holder["id"] = self.store.create("做网页", str(self.cwd), mode="harness")["id"]
        self.run_like_worker(engine, task)
        saved = self.store.get(task)
        self.assertEqual(saved["state"], "passed")
        self.assertTrue(saved["runs"][0].get("interrupted"))
        self.assertIn("方向错了", harness.calls[1]["prompt"])
        self.assertIn("interrupted_for_input", [e["kind"] for e in self.store.events(task)])

    def test_stop_button_still_wins_over_interrupt(self):
        holder = {}
        def both():
            holder["e"].guide(holder["id"], "换个思路", interrupt=True)
            holder["e"].cancel(holder["id"])
        engine = holder["e"] = self.engine(Fake([]), During([result()], {0: both}))
        task = holder["id"] = self.store.create("x", str(self.cwd), mode="harness")["id"]
        self.run_like_worker(engine, task)
        self.assertEqual(self.store.get(task)["state"], "cancelled")

    def test_guidance_on_finished_task_continues_and_is_reviewed(self):
        codex = Fake([PLAN, review(passed=True), review(passed=True)])
        harness = Fake([result("v1"), result("v2")])
        engine = self.engine(codex, harness)
        task = self.store.create("写文档", str(self.cwd))["id"]
        engine.run_task(task)
        self.assertEqual(self.store.get(task)["state"], "passed")
        engine.guide(task, "再加一节常见问题")
        engine.pending.get_nowait()
        engine.run_task(task)
        saved = self.store.get(task)
        self.assertEqual((saved["state"], saved["repair_count"]), ("passed", 0))
        self.assertIn("再加一节常见问题", codex.calls[-1]["prompt"])  # the reviewer judges the updated requirement
        self.assertEqual(len(codex.calls), 3)  # no new plan

    def test_guidance_during_review_goes_to_executor_first(self):
        holder = {}
        codex = During([PLAN, review(passed=True), review(passed=True)], {1: lambda: holder["e"].guide(holder["id"], "补上单元测试")})
        harness = Fake([result("v1"), result("v2")])
        engine = holder["e"] = self.engine(codex, harness)
        task = holder["id"] = self.store.create("x", str(self.cwd))["id"]
        self.run_like_worker(engine, task)
        saved = self.store.get(task)
        self.assertEqual(saved["state"], "passed")
        self.assertEqual([(r["role"], r["stage"]) for r in saved["runs"]],
                         [("planner", "plan"), ("executor", "execute"), ("reviewer", "review"), ("executor", "guide"), ("reviewer", "review")])

    def test_study_message_sent_while_replying_waits_its_turn(self):
        holder = {}
        harness = During([result("A1"), result("A2")], {0: lambda: holder["e"].add_message(holder["id"], "第二句", "harness")})
        engine = holder["e"] = self.engine(Fake([]), harness)
        task = holder["id"] = engine.submit_study("第一句", "harness")["id"]
        engine.pending.get_nowait()
        self.run_like_worker(engine, task)
        saved = self.store.get(task)
        self.assertEqual([m["text"] for m in saved["messages"]], ["第一句", "A1", "第二句", "A2"])
        self.assertEqual(harness.calls[1]["prompt"], "第二句")
        self.assertEqual(saved["state"], "passed")

    def test_study_interrupt_replaces_the_reply(self):
        holder = {}
        harness = During([result("A1"), result("A2")],
                         {0: lambda: holder["e"].add_message(holder["id"], "算了，换个问题", "harness", interrupt=True)})
        engine = holder["e"] = self.engine(Fake([]), harness)
        task = holder["id"] = engine.submit_study("第一句", "harness")["id"]
        engine.pending.get_nowait()
        self.run_like_worker(engine, task)
        saved = self.store.get(task)
        self.assertEqual([m["text"] for m in saved["messages"]], ["第一句", "算了，换个问题", "A2"])
        self.assertIn("算了，换个问题", harness.calls[1]["prompt"])


if __name__ == "__main__":
    unittest.main()
