"""Opt-in real local-agent acceptance, using only a new fixture directory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_workbench.engine import Engine
from agent_workbench.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mode", choices=["harness", "codex", "combined"], default="combined")
    parser.add_argument("--patch", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    root = args.root.resolve() / (args.mode + "-" + uuid.uuid4().hex[:8])
    cwd = root / "中文 空格 项目"
    cwd.mkdir(parents=True)
    marker = "WORKBENCH_OK_" + uuid.uuid4().hex
    (cwd / "acceptance.py").write_text(
        "from pathlib import Path\n"
        f"assert Path('result.txt').read_text(encoding='utf-8').strip() == {marker!r}\n"
        "print('ACCEPTANCE_OK')\n", encoding="utf-8")
    store = Store(root / "data")
    engine = Engine(store, args.patch)
    task = store.create(
        f"这是本地工作台集成验收。只在当前目录创建 result.txt，UTF-8 内容为 {marker}，可带末尾换行。"
        "不要修改 acceptance.py。运行 python acceptance.py，报告实际结果。不要开展其他工作。",
        str(cwd), args.mode, args.timeout, [[sys.executable, "acceptance.py"]])
    print(f"run={root}\ntask={task['id']}", flush=True)
    engine.run_task(task["id"], threading.Event())
    final = store.get(task["id"])
    passed = final["state"] == "passed" and (cwd / "result.txt").is_file() and (cwd / "result.txt").read_text(encoding="utf-8").strip() == marker
    evidence = {"passed": passed, "task": store.summary(task["id"])}
    (root / "summary.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": passed, "state": final["state"], "repairs": final["repair_count"],
                      "stop_reason": final["stop_reason"], "summary": str(root / "summary.json")}, ensure_ascii=False, indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
