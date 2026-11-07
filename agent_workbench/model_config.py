from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess


PUBLIC_FIELDS = ("policy", "provider", "model", "reasoning_effort", "endpoint_host", "source", "fingerprint", "captured_at")


def public_model(snapshot):
    return {key: snapshot.get(key) for key in PUBLIC_FIELDS} if snapshot else None


def desktop_source():
    home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
    return home.resolve() / "profiles/desktop/cordis.patch.yml"


def capture_model(install, source=None):
    source = Path(source).resolve() if source else desktop_source()
    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    command = [str(Path(install) / "DeepSeek Harness.exe"), "--expose-internals",
               str(Path(__file__).with_name("harness_config.cjs")), str(install), str(source)]
    try:
        process = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        config = json.loads(process.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("无法读取 Harness 桌面模型配置；请检查安装位置和配置文件。") from None
    if not isinstance(config, dict):
        raise ValueError("Harness 模型配置格式无效")
    if process.returncode != 0 or config.get("error"):
        raise ValueError(config.get("message", "Harness 模型配置读取失败"))
    patch = config.get("patch")
    selection = config.get("selection", {})
    if not isinstance(patch, list) or not selection.get("provider") or not selection.get("model"):
        raise ValueError("Harness 模型配置缺少必要字段")
    fingerprint = hashlib.sha256(json.dumps(patch, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return dict(policy="explicit_patch" if source != desktop_source() else "follow_desktop",
                provider=selection["provider"], model=selection["model"],
                reasoning_effort=selection.get("reasoningEffort"), endpoint_host=config.get("endpoint_host"),
                source=str(source), fingerprint=fingerprint, patch=patch,
                captured_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("patch"), list):
        raise ValueError("任务没有可恢复的 Harness 模型快照，请新建任务")
    fingerprint = hashlib.sha256(json.dumps(snapshot["patch"], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    if fingerprint != snapshot.get("fingerprint"):
        raise ValueError("任务的模型配置快照已发生变化；已停止以避免意外切换通道")
    selected = [row.get("config") for row in snapshot["patch"] if isinstance(row, dict) and row.get("id") == "agent-default-model"]
    if (len(selected) != 1 or not isinstance(selected[0], dict)
            or selected[0].get("provider") != snapshot.get("provider")
            or selected[0].get("model") != snapshot.get("model")
            or selected[0].get("reasoningEffort") != snapshot.get("reasoning_effort")):
        raise ValueError("任务的模型选择与配置快照不一致")
    return snapshot
