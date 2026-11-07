from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_workbench.adapters import Codex, Harness, describe_input
from test_workbench import model_snapshot


def replay(adapter_run, fixture):
    events = []
    def run(command, cwd, prompt, timeout, cancel, emit, parser, env=None):
        out = dict(ok=True, transport_ok=True, tool_errors=[], errors=[], final=None, session_id=None, reason=None)
        for event in fixture:
            parser(event, out, emit)
        return out
    with patch("agent_workbench.adapters.run_process", run), patch("agent_workbench.adapters.codex_program", return_value=["codex"]):
        result = adapter_run(lambda kind, data: events.append((kind, data)))
    return result, events


class ProcessViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_harness_steps_show_commands_paths_and_notes_but_never_thinking(self):
        fixture = [
            dict(type="session", sessionId="s1"),
            dict(type="thinking", text="PRIVATE_THINKING"),
            dict(type="text", text="我先看看项目结构。"),
            dict(type="tool_call", callId="c1", tool="pwsh", input={"command": "python -m unittest -q"}),
            dict(type="tool_result", callId="c1", status="completed", result="Ran 5 tests\nOK"),
            dict(type="tool_call", callId="c2", tool="write", input={"path": "ledger.py", "content": "a\nb\nc"}),
            dict(type="tool_call", callId="c3", tool="pwsh", input={"command": "curl -H 'Authorization: Bearer SECRET123' x"}),
            dict(type="status", phase="step_end", usage={"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 60}),
            dict(type="status", phase="step_end", usage={"inputTokens": 50, "outputTokens": 5}),
            dict(type="final", sessionId="s1", reason=dict(kind="completed"), text="done"),
        ]
        result, events = replay(lambda emit: Harness(self.cwd / "p").run("t", str(self.cwd), 10, threading.Event(), emit,
                                                                         model_config=model_snapshot()), fixture)
        dump = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("PRIVATE_THINKING", dump)
        self.assertNotIn("SECRET123", dump)
        self.assertIn(("note", {"text": "我先看看项目结构。"}), events)
        calls = [d for k, d in events if k == "tool_call"]
        self.assertEqual([c["summary"] for c in calls[:2]], ["python -m unittest -q", "ledger.py  ·  3 行"])
        self.assertIn("Ran 5 tests", next(d for k, d in events if k == "tool_result")["preview"])
        self.assertEqual(result["usage"], {"input": 150, "output": 25, "cached": 60})
        self.assertNotIn("step_end", dump)

    def test_codex_steps_show_command_output_and_file_changes(self):
        fixture = [
            dict(type="thread.started", thread_id="t1"),
            {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE_THINKING"}},
            {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "python -m unittest", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "python -m unittest",
                                                "status": "completed", "exit_code": 0, "aggregated_output": "OK"}},
            {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "status": "completed",
                                                "changes": [{"path": "ledger.py", "kind": "update"}]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "审核完成"}},
            {"type": "turn.completed", "usage": {"input_tokens": 17000, "cached_input_tokens": 5000, "output_tokens": 300}},
        ]
        result, events = replay(lambda emit: Codex().run("t", str(self.cwd), 10, threading.Event(), emit), fixture)
        self.assertNotIn("PRIVATE_THINKING", json.dumps(events, ensure_ascii=False))
        tools = [d for k, d in events if k == "tool_event"]
        self.assertEqual(tools[1]["preview"], "OK")
        self.assertEqual(tools[0]["summary"], "python -m unittest")
        self.assertEqual(tools[2]["summary"], "update ledger.py")
        self.assertIn(("note", {"text": "审核完成"}), events)
        self.assertEqual(result["usage"], {"input": 17000, "output": 300, "cached": 5000})

    def test_describe_input_fallbacks(self):
        self.assertEqual(describe_input("glob", {"pattern": "**/*.py", "path": "src"}), "**/*.py  ·  src")
        self.assertEqual(describe_input("x", {}), "")
        self.assertTrue(len(describe_input("x", {"blob": "y" * 999})) < 320)


if __name__ == "__main__":
    unittest.main()
