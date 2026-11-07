from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading

from .engine import Engine
from .server import InstanceLock, serve
from .store import Store


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="本地 Agent 工作台：复用本机 Codex / Harness 配置")
    parser.add_argument("--data", type=Path, default=root / ".agent-workbench")
    parser.add_argument("--patch", type=Path, help="显式指定 Harness 模型补丁；默认跟随桌面配置")
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve", help="启动本地网页界面")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--open", action="store_true")
    run = sub.add_parser("run", help="前台运行一个任务")
    run.add_argument("--task", required=True)
    run.add_argument("--cwd", required=True)
    run.add_argument("--mode", choices=["combined", "codex", "harness"], default="combined")
    run.add_argument("--timeout", type=float, default=600)
    run.add_argument("--checks", default="[]", help='显式验收命令的 JSON 参数数组')
    sub.add_parser("list", help="列出已有任务")
    show = sub.add_parser("show", help="输出任务与交接摘要")
    show.add_argument("id")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            serve(args.data, args.patch, args.port, args.open)
            return 0
        store = Store(args.data)
        if args.command == "list":
            print(json.dumps([{k: t[k] for k in ("id", "state", "mode", "repair_count", "prompt")} for t in store.list()], ensure_ascii=False, indent=2))
            return 0
        if args.command == "show":
            print(json.dumps(store.summary(args.id), ensure_ascii=False, indent=2))
            return 0
        with InstanceLock(store.root / "instance.lock"):
            store.recover()
            engine = Engine(store, args.patch)
            task = store.create(args.task, args.cwd, args.mode, args.timeout, json.loads(args.checks))
            print(f"任务：{task['id']}", flush=True)
            cancel = threading.Event()
            try:
                # The runner receives cancellation on Ctrl+C through the outer handler.
                import signal
                previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
                engine.run_task(task["id"], cancel)
            finally:
                signal.signal(signal.SIGINT, previous)
            print(json.dumps(store.summary(task["id"]), ensure_ascii=False, indent=2))
            return 0 if store.get(task["id"])["state"] == "passed" else 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"工作台错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
