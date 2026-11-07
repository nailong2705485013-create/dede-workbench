from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest

from agent_workbench.engine import CHUNKING, CONTINUE_PROMPT, Engine
from agent_workbench.store import Store
from test_workbench import Fake, model_snapshot, result


class Gate:
    """Fake agent that blocks until released, recording how many run at once."""
    def __init__(self):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.started = []

    def capture_model(self):
        return model_snapshot()

    def run(self, prompt, cwd, timeout, cancel, on_event, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.append(cwd)
        on_event("session", {"session_id": "s"})
        while not (self.release.is_set() or cancel.is_set()):
            time.sleep(.02)
        with self.lock:
            self.active -= 1
        return result("ok")


def wait_for(check, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(.05)
    return False


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "root").mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, name):
        path = self.base / name
        path.mkdir(exist_ok=True)
        return str(path)

    def test_work_runs_three_at_once_and_one_per_folder(self):
        gate = Gate()
        engine = Engine(self.store, adapters={"codex": gate, "harness": gate}, study_root=self.base / "root")
        engine.start()
        try:
            ids = [engine.submit(prompt="x", cwd=self.folder(f"p{i}"), mode="harness")["id"] for i in range(4)]
            same = engine.submit(prompt="y", cwd=self.folder("p0"), mode="harness")["id"]
            self.assertTrue(wait_for(lambda: gate.active == 3))
            time.sleep(.5)
            self.assertEqual(gate.active, 3)  # the 4th folder and the duplicate folder wait
            self.assertEqual(self.store.get(ids[3])["state"], "queued")
            self.assertEqual(self.store.get(same)["state"], "queued")
            self.assertEqual(engine.scheduler_state()["work"], 3)
            gate.release.set()
            self.assertTrue(wait_for(lambda: all(self.store.get(i)["state"] == "passed" for i in ids + [same])))
            self.assertEqual(gate.peak, 3)
        finally:
            gate.release.set()
            engine.close()

    def test_study_is_not_blocked_by_running_work(self):
        gate = Gate()
        chat = Fake([result("你好")])
        engine = Engine(self.store, adapters={"codex": chat, "harness": gate}, study_root=self.base / "root")
        engine.start()
        try:
            for i in range(3):
                engine.submit(prompt="x", cwd=self.folder(f"w{i}"), mode="harness")
            self.assertTrue(wait_for(lambda: gate.active == 3))
            talk = engine.submit_study("hi", "codex")["id"]
            self.assertTrue(wait_for(lambda: self.store.get(talk)["state"] == "passed"))
            self.assertEqual(gate.active, 3)
        finally:
            gate.release.set()
            engine.close()

    def test_max_tokens_is_continued_once_in_the_same_session(self):
        cut = dict(result("", ok=False), reason="max-tokens", errors=['{"kind": "max-tokens"}'])
        harness = Fake([cut, result("写完了")])
        engine = Engine(self.store, adapters={"codex": Fake([]), "harness": harness}, study_root=self.base / "root")
        task = self.store.create("写个大页面", self.folder("big"), mode="harness")
        engine.run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual(saved["state"], "passed")
        self.assertEqual(harness.calls[1]["prompt"], CONTINUE_PROMPT)
        self.assertEqual(harness.calls[1]["session_id"], "s-test")
        self.assertIn(CHUNKING, harness.calls[0]["prompt"])
        self.assertEqual([r.get("auto_continue") for r in saved["runs"]], [False, True])
        self.assertEqual(saved["runtime_failures"], 0)

    def test_second_cut_off_pauses_with_an_explanation(self):
        cut = dict(result("", ok=False), reason="max-tokens", errors=['{"kind": "max-tokens"}'])
        harness = Fake([cut, dict(cut)])
        engine = Engine(self.store, adapters={"codex": Fake([]), "harness": harness}, study_root=self.base / "root")
        task = self.store.create("写个大页面", self.folder("big2"), mode="harness")
        engine.run_task(task["id"])
        saved = self.store.get(task["id"])
        self.assertEqual((saved["state"], saved["runtime_failures"], len(harness.calls)), ("runtime_failed", 1, 2))
        self.assertIn("自动续写 1 次后仍被截断", saved["stop_reason"])


if __name__ == "__main__":
    unittest.main()
