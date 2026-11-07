from __future__ import annotations

import json
import os
from pathlib import Path
import sys

HARNESS_ACL_HINT = ("Harness 的 Windows 沙箱需要对项目文件夹拥有“完全控制”（含 WRITE_OWNER），"
                    "这个文件夹只有“修改”权限，命令工具会报 SetNamedSecurityInfoW failed (Win32 5)。"
                    "请换用用户目录下的文件夹，或在文件夹“属性 → 安全”中给自己的账户加“完全控制”后再恢复；"
                    "工作台不会自动修改权限。")


def detect_checks(root):
    """A test command chosen from files that exist in the project, never from model text. [] when nothing is found."""
    root = Path(root)
    tests = root / "tests"
    try:
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").is_file() else ""
    except (OSError, UnicodeDecodeError):
        pyproject = ""
    if (root / "pytest.ini").is_file() or "[tool.pytest" in pyproject:
        return [[sys.executable, "-m", "pytest", "-q"]]
    if tests.is_dir() and any(tests.glob("test*.py")):
        top = ["-t", "."] if (tests / "__init__.py").is_file() else []
        return [[sys.executable, "-m", "unittest", "discover", "-s", "tests", *top, "-q"]]
    if any(root.glob("test*.py")):
        return [[sys.executable, "-m", "unittest", "discover", "-q"]]
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8")) if (root / "package.json").is_file() else {}
    except (OSError, ValueError):
        package = {}
    script = (package.get("scripts") or {}).get("test") if isinstance(package, dict) else None
    if isinstance(script, str) and script.strip() and "no test specified" not in script:
        return [["npm.cmd" if os.name == "nt" else "npm", "test", "--silent"]]
    if (root / "go.mod").is_file():
        return [["go", "test", "./..."]]
    if (root / "Cargo.toml").is_file():
        return [["cargo", "test", "-q"]]
    return []


def show_command(argv):
    return " ".join("python" if part == sys.executable else part for part in argv)


def harness_can_grant(path):
    """Return True if this process may edit the folder's DACL and label, as Harness's sandbox does.

    Only opens a handle with the needed access rights; nothing is changed.
    """
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    access = 0x00020000 | 0x00040000 | 0x00080000  # READ_CONTROL | WRITE_DAC | WRITE_OWNER
    handle = kernel.CreateFileW(str(path), access, 7, None, 3, 0x02000000, None)  # OPEN_EXISTING, BACKUP_SEMANTICS
    if handle in (None, w.HANDLE(-1).value):
        if ctypes.get_last_error() == 5:
            return False
        raise OSError(ctypes.get_last_error(), "无法检查文件夹权限")
    kernel.CloseHandle(handle)
    return True
