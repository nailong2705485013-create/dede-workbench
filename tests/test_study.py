from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from agent_workbench import study
from agent_workbench.engine import Engine
from agent_workbench.store import Store
from test_workbench import Fake, result


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "dede工作台"
        self.root.mkdir()
        self.store = Store(self.base / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, codex, harness):
        return Engine(self.store, adapters={"codex": codex, "harness": harness}, study_root=self.root)

    def chat(self, engine, text, executor):
        task = engine.submit_study(text, executor)
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        return self.store.get(task["id"])

    def reply(self, engine, task_id, text, executor):
        engine.add_message(task_id, text, executor)
        engine.pending.get_nowait()
        engine.run_task(task_id)
        return self.store.get(task_id)

    def test_root_must_be_set_up_first(self):
        engine = Engine(self.store, adapters={"codex": Fake([]), "harness": Fake([])}, study_root=self.base / "missing")
        with self.assertRaisesRegex(ValueError, "授权"):
            engine.submit_study("hi", "harness")
        self.assertEqual(self.store.list(), [])
        self.assertFalse((self.base / "missing").exists())

    def test_folder_named_by_time_with_output_and_transcript(self):
        harness = Fake([result("你好！")])
        task = self.chat(self.engine(Fake([]), harness), "hi", "harness")
        folder = Path(task["cwd"])
        self.assertEqual(folder.parent, (self.root / "study").resolve())
        self.assertRegex(folder.name, r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
        self.assertTrue((folder / "output").is_dir())
        self.assertEqual(task["state"], "passed")
        self.assertEqual([(m["role"], m.get("executor")) for m in task["messages"]], [("user", None), ("assistant", "harness")])
        self.assertEqual(task["seen"], {"harness": 2})
        self.assertIsNotNone(task["harness_model"])
        self.assertTrue(harness.calls[0]["prompt"].startswith(study.RULES))
        self.assertIn("你好！", (folder / study.TRANSCRIPT).read_text(encoding="utf-8"))

    def test_switching_models_hands_over_only_unseen_messages(self):
        codex, harness = Fake([result("C1")]), Fake([result("H1"), result("H2"), result("H3")])
        engine = self.engine(codex, harness)
        task = self.chat(engine, "第一问", "harness")
        task = self.reply(engine, task["id"], "第二问", "codex")
        first_codex = codex.calls[0]["prompt"]
        self.assertTrue(first_codex.startswith(study.RULES))
        for text in ("### 用户\n第一问", "### Harness\nH1", "用户的最新消息：\n第二问"):
            self.assertIn(text, first_codex)
        self.assertIsNone(codex.calls[0].get("session_id"))
        task = self.reply(engine, task["id"], "第三问", "harness")
        back = harness.calls[1]
        self.assertEqual(back["session_id"], "s-test")
        self.assertFalse(back["prompt"].startswith(study.RULES))
        self.assertNotIn("第一问", back["prompt"])
        for text in ("### 用户\n第二问", "### Codex\nC1", "用户的最新消息：\n第三问"):
            self.assertIn(text, back["prompt"])
        self.assertEqual(task["seen"], {"harness": 6, "codex": 4})
        same = self.reply(engine, task["id"], "第四问", "harness")
        self.assertEqual(harness.calls[2]["prompt"], "第四问")
        self.assertEqual(len(same["messages"]), 8)

    def test_long_history_is_trimmed_with_pointer_to_transcript(self):
        task = {"messages": [dict(role="user", text="x" * 30000, at="t"), dict(role="assistant", executor="harness", text="y" * 30000, at="t"),
                             dict(role="user", text="最新", at="t")], "seen": {}}
        prompt = study.build_prompt(task, "codex", fresh=True)
        self.assertIn(study.TRANSCRIPT, prompt)
        self.assertLess(len(prompt), 45000)
        self.assertTrue(prompt.endswith("最新"))

    def test_follow_up_rules(self):
        harness = Fake([result("ok")])
        engine = self.engine(Fake([]), harness)
        task = engine.submit_study("hi", "harness")
        queued = engine.add_message(task["id"], "again", "harness")  # waits behind the current reply
        self.assertEqual((queued["state"], len(queued["messages"])), ("queued", 2))
        with self.assertRaisesRegex(ValueError, "未知模型"):
            engine.add_message(task["id"], "again", "gpt")
        work_dir = self.base / "proj"
        work_dir.mkdir()
        work = self.store.create("job", str(work_dir), mode="harness")
        self.store.update(work["id"], state="passed")
        guided = engine.add_message(work["id"], "more", "harness")  # Work follow-ups become guidance
        self.assertEqual((guided["state"], guided["stage"], guided["guidance"][0]["text"]), ("queued", "guide", "more"))
        with self.assertRaisesRegex(ValueError, "接管"):
            self.store.update(task["id"], state="cancelled")
            engine.resume(task["id"], takeover=True)

    def test_failed_turn_keeps_message_and_can_be_retried(self):
        harness = Fake([result("x", ok=False), result("好了")])
        engine = self.engine(Fake([]), harness)
        task = self.chat(engine, "hi", "harness")
        self.assertEqual((task["state"], len(task["messages"])), ("runtime_failed", 1))
        engine.resume(task["id"])
        engine.pending.get_nowait()
        engine.run_task(task["id"])
        task = self.store.get(task["id"])
        self.assertEqual((task["state"], task["messages"][-1]["text"]), ("passed", "好了"))

    def test_delete_removes_study_folder_but_never_work_folder(self):
        engine = self.engine(Fake([]), Fake([result("ok")]))
        task = self.chat(engine, "hi", "harness")
        folder = Path(task["cwd"])
        engine.delete(task["id"])
        self.assertFalse(folder.exists())
        with self.assertRaises(KeyError):
            self.store.get(task["id"])
        self.assertEqual(self.store.events(task["id"]), [])
        work_dir = self.base / "proj"
        work_dir.mkdir()
        (work_dir / "keep.txt").write_text("x", encoding="utf-8")
        work = self.store.create("job", str(work_dir), mode="harness")
        with self.assertRaisesRegex(ValueError, "先停止"):
            engine.delete(work["id"])
        self.store.update(work["id"], state="cancelled")
        engine.delete(work["id"])
        self.assertTrue((work_dir / "keep.txt").exists())

    def test_delete_refuses_folder_outside_study_dir(self):
        outside = self.base / "outside"
        outside.mkdir()
        task = self.store.create_study("hi", "harness", outside)
        self.store.update(task["id"], state="passed")
        with self.assertRaisesRegex(ValueError, "拒绝"):
            self.engine(Fake([]), Fake([])).delete(task["id"])
        self.assertTrue(outside.exists())
        self.store.get(task["id"])

    def test_deleted_queued_task_does_not_stop_worker(self):
        engine = self.engine(Fake([]), Fake([result("ok")]))
        engine.start()
        try:
            engine.pending.put("missing-id")
            task = engine.submit_study("hi", "harness")
            for _ in range(100):
                if self.store.get(task["id"])["state"] == "passed":
                    break
                import time
                time.sleep(.05)
            self.assertEqual(self.store.get(task["id"])["state"], "passed")
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
