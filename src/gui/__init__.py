"""Optional PySide6 (Qt 6) desktop UI and system tray for Völundr."""

from __future__ import annotations

from pathlib import Path


def launch_gui(
    config_path: Path,
    *,
    start_minimized: bool = False,
    auto_watch: bool | None = None,
) -> int:
    """Launch the PySide6 desktop window and system tray icon."""
    from src.gui.app import run_gui_app

    return run_gui_app(
        config_path,
        start_minimized=start_minimized,
        auto_watch=auto_watch,
    )
