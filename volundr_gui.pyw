#!/usr/bin/env python3
"""
Windowless launcher for the Völundr desktop UI & system tray on Windows.

Double-click `volundr_gui.pyw` (or run with `.venv\\Scripts\\pythonw.exe volundr_gui.pyw`)
to open Völundr without a background console window. If launched with system Python while
a local `.venv` exists, automatically delegates to `.venv`'s `pythonw.exe`.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def _reexec_in_venv_if_needed() -> bool:
    """Delegate to local `.venv` Python if launched from outside `.venv`."""
    venv_pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    venv_py = ROOT / ".venv" / "bin" / "python"
    target = venv_pyw if sys.platform.startswith("win") else venv_py

    if not target.is_file():
        return False

    cur_prefix = Path(sys.prefix).resolve()
    venv_root = (ROOT / ".venv").resolve()
    if cur_prefix == venv_root:
        return False

    subprocess.Popen([str(target), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(ROOT))
    return True


def main() -> int:
    if _reexec_in_venv_if_needed():
        return 0

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    import main as cli_main

    return cli_main.main(["gui", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
