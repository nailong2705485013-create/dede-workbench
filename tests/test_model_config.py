from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_workbench.adapters import Harness, INSTALL
from agent_workbench.engine import Engine
from agent_workbench.model_config import capture_model, public_model, validate_snapshot
from agent_workbench.store import Store
from test_workbench import Fake, PLAN, model_snapshot, result, review


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, mode="harness"):
        return self.store.create("fixture task", str(self.root), mode)

    def engine(self, harness, codex=None):
        return Engine(self.store, adapters={"harness": harness, "codex": codex or Fake([])})

    def test_default_adapter_follows_desktop(self):
        self.assertIsNone(Harness().patch)

    def test_adapter_resume_without_original_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            Harness().run("fixture", self.root, 10, threading.Event(), lambda *_: None, session_id="old-session")

    def test_new_tasks_capture_when_started(self):
        first, second = self.task(), self.task()
        harness = Fake([result(), result()])
        harness.capture_model = lambda: model_snapshot("route-a")
        engine = self.engine(harness)
        engine.run_task(first["id"])
        harness.capture_model = lambda: model_snapshot("route-b")
        engine.run_task(second["id"])
        self.assertEqual([c["model_config"]["provider"] for c in harness.calls], ["route-a", "route-b"])

    def test_repair_keeps_original_snapshot_after_desktop_changes(self):
        harness = Fake([result(), result()])
        original = harness.run
        def run(*args, **kwargs):
            value = original(*args, **kwargs)
            harness.capture_model = lambda: model_snapshot("changed-desktop")
            return value
        harness.run = run
        task = self.task("combined")
        self.engine(harness, Fake([PLAN, review(), review(True)])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "passed")
        self.assertEqual([c["model_config"]["provider"] for c in harness.calls], ["test-route"] * 2)
        self.assertEqual(harness.calls[-1]["session_id"], "s-test")

    def test_resume_reuses_persisted_snapshot_without_reading_desktop(self):
        task = self.task()
        self.engine(Fake([result(ok=False)])).run_task(task["id"])
        harness = Fake([result()])
        harness.capture_model = lambda: self.fail("resume must not read desktop settings")
        engine = self.engine(harness)
        engine.resume(task["id"])
        engine.run_task(task["id"])
        final = self.store.get(task["id"])
        self.assertEqual(final["state"], "passed")
        self.assertEqual(final["harness_model"], harness.calls[0]["model_config"])

    def test_config_error_stops_without_fallback_or_repair(self):
        harness = Fake([])
        def fail():
            raise ValueError("desktop config unavailable")
        harness.capture_model = fail
        task = self.task()
        self.engine(harness).run_task(task["id"])
        final = self.store.get(task["id"])
        self.assertEqual((final["state"], final["repair_count"], final["runtime_failures"]), ("runtime_failed", 0, 1))
        self.assertEqual(harness.calls, [])

    def test_legacy_session_without_snapshot_cannot_silently_switch(self):
        task = self.task()
        self.store.update(task["id"], sessions={"harness:executor": "legacy-account"})
        harness = Fake([])
        self.engine(harness).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "runtime_failed")
        self.assertEqual(harness.calls, [])

    def test_corrupted_or_empty_snapshot_cannot_fall_back(self):
        corrupted = model_snapshot()
        corrupted["patch"][0]["config"]["provider"] = "other"
        for snapshot in ({}, corrupted):
            task = self.task()
            self.store.update(task["id"], harness_model=snapshot)
            harness = Fake([])
            self.engine(harness).run_task(task["id"])
            self.assertEqual(self.store.get(task["id"])["state"], "runtime_failed")
            self.assertEqual(harness.calls, [])

    def test_codex_only_does_not_require_harness_config(self):
        harness = Fake([])
        harness.capture_model = lambda: self.fail("Codex must not read Harness config")
        task = self.task("codex")
        self.engine(harness, Fake([result()])).run_task(task["id"])
        self.assertEqual(self.store.get(task["id"])["state"], "passed")

    def test_summary_hides_internal_overlay(self):
        task = self.task()
        self.store.update(task["id"], harness_model=model_snapshot())
        summary = self.store.summary(task["id"])
        self.assertNotIn("patch", summary["harness_model"])
        self.assertEqual(summary["harness_model"]["provider"], "test-route")

    def test_metadata_must_match_snapshot(self):
        value = model_snapshot()
        value["reasoning_effort"] = "max"
        with self.assertRaises(ValueError):
            validate_snapshot(value)


class HarnessConfigParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = Path(os.environ.get("WORKBENCH_HARNESS_INSTALL", INSTALL))
        if not (cls.install / "DeepSeek Harness.exe").is_file():
            raise unittest.SkipTest("Harness runtime needed for its bundled YAML parser")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "profiles/desktop/cordis.patch.yml"
        self.source.parent.mkdir(parents=True)
        self.rows = [
            {"id": "unrelated-ui", "config": {"private": "DO_NOT_COPY"}},
            {"id": "agent-default-model", "config": {"provider": "route-a", "model": "model-a", "reasoningEffort": "max"}},
            {"id": "llm-pi-ai", "config": {"providers": {
                "route-a": {"api": "openai-completions", "apiKeyEnv": "FIXTURE_ROUTE_KEY",
                    "baseURL": "https://example.invalid/v1", "compat": {"supportsDeveloperRole": False},
                    "models": [{"id": "model-a", "contextWindow": 1000000, "maxTokens": 32000}]},
                "unselected-route": {"apiKey": "UNSELECTED_INLINE_SECRET"}}}},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, rows=None):
        self.source.write_text(json.dumps(self.rows if rows is None else rows), encoding="utf-8")
        with patch.dict(os.environ, {"DSH_HOME": str(self.root), "FIXTURE_ROUTE_KEY": "NEVER_RESOLVE_THIS_KEY"}):
            return capture_model(self.install)

    def test_only_selected_nonsecret_provider_parameters_are_copied(self):
        snapshot = self.capture()
        validate_snapshot(snapshot)
        self.assertEqual(snapshot["policy"], "follow_desktop")
        self.assertEqual((snapshot["provider"], snapshot["model"], snapshot["reasoning_effort"]), ("route-a", "model-a", "max"))
        serialized = json.dumps(snapshot)
        for forbidden in ("DO_NOT_COPY", "unselected-route", "UNSELECTED_INLINE_SECRET", "NEVER_RESOLVE_THIS_KEY"):
            self.assertNotIn(forbidden, serialized)
        providers = next(r["config"]["providers"] for r in snapshot["patch"] if r["id"] == "llm-pi-ai")
        self.assertEqual(set(providers), {"route-a"})
        self.assertEqual(providers["route-a"]["models"][0]["contextWindow"], 1000000)
        self.assertTrue(next(r["disabled"] for r in snapshot["patch"] if r["id"] == "deepseek-account"))
        self.assertNotIn("patch", public_model(snapshot))

    def test_selected_inline_secret_is_rejected_without_echoing_it(self):
        self.rows[2]["config"]["providers"]["route-a"]["apiKey"] = "PRIVATE_FIXTURE_SECRET"
        with self.assertRaises(ValueError) as caught:
            self.capture()
        self.assertNotIn("PRIVATE_FIXTURE_SECRET", str(caught.exception))

    def test_missing_selection_or_provider_or_model_fails(self):
        for change in ("selection", "provider", "model"):
            rows = copy.deepcopy(self.rows)
            if change == "selection":
                rows.pop(1)
            elif change == "provider":
                rows[1]["config"]["provider"] = "not-configured"
            else:
                rows[1]["config"]["model"] = "not-configured"
            with self.assertRaises(ValueError):
                self.capture(rows)

    def test_endpoint_credentials_rejected(self):
        self.rows[2]["config"]["providers"]["route-a"]["baseURL"] = "https://example.invalid/?key=PRIVATE_FIXTURE_SECRET"
        with self.assertRaises(ValueError) as caught:
            self.capture()
        self.assertNotIn("PRIVATE_FIXTURE_SECRET", str(caught.exception))

    def test_yaml_data_format_and_builtin_account(self):
        self.source.write_text("- id: agent-default-model\n  config:\n    provider: deepseek-account\n    model: deepseek-flash\n", encoding="utf-8")
        snapshot = capture_model(self.install, self.source)
        self.assertEqual(snapshot["policy"], "explicit_patch")
        self.assertFalse(next(r["disabled"] for r in snapshot["patch"] if r["id"] == "deepseek-account"))

    def test_executable_yaml_and_dynamic_expressions_rejected(self):
        self.source.write_text("- id: agent-default-model\n  config: !!js 'process.env.PRIVATE_KEY'\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            capture_model(self.install, self.source)
        self.rows[2]["config"]["providers"]["route-a"]["apiKeyEnv"] = "${{ env.PRIVATE_KEY }}"
        with self.assertRaises(ValueError):
            self.capture()


if __name__ == "__main__":
    unittest.main()
