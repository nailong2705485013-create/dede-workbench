"""Verify same-cwd cross-process session memory through the actual adapters."""
import argparse
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_workbench.adapters import Codex, Harness

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--executor", choices=["codex", "harness"], default="codex")
args = parser.parse_args()
root = args.root.resolve() / ("session-" + args.executor + "-" + uuid.uuid4().hex[:8])
root.mkdir(parents=True)
memory = "MEMORY_" + uuid.uuid4().hex
schema = root / "schema.json"
schema.write_text(json.dumps({"type":"object","properties":{"memory":{"type":"string"}},"required":["memory"],"additionalProperties":False}), encoding="utf-8")
adapter = Codex() if args.executor == "codex" else Harness()
model = {"model_config": adapter.capture_model()} if args.executor == "harness" else {}
events = []
def emit(kind, data):
    events.append({"kind":kind,"data":data})
first = adapter.run(f"只记住会话校验码 {memory}。不要调用工具、不要写文件。只回复 JSON，memory 为校验码。", str(root), 180,
                    threading.Event(), emit, readonly=True, schema=schema, **model)
second = None
if first["ok"] and first["session_id"]:
    second = adapter.run("从当前对话回忆上一轮校验码，不要读文件、不要调用工具。只回复 JSON，memory 为校验码。", str(root), 180,
                         threading.Event(), emit, session_id=first["session_id"], readonly=True, schema=schema, **model)
passed = bool(second and second["ok"] and first["session_id"] == second["session_id"] and memory in second["final"])
(root / "summary.json").write_text(json.dumps({"passed":passed,"first":first,"second":second,"events":events},ensure_ascii=False,indent=2), encoding="utf-8")
print(json.dumps({"passed":passed,"evidence":str(root / "summary.json"), "second_errors":(second or {}).get("errors")},ensure_ascii=False),flush=True)
raise SystemExit(0 if passed else 1)
