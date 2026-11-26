"""Unit and headless offscreen Qt smoke tests for the Völundr PySide6 desktop GUI."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main as cli_main
from src.config_loader import (
    ConfigError,
    load_config,
    save_advanced_settings,
    save_basic_settings,
)
from src.gui.updater import (
    check_for_updates,
    clear_update_cache,
    is_newer_version,
    parse_releases_atom_xml,
    parse_semver,
    perform_git_update,
)
from src.models import PostProcessAction, RoutingOutcome
from src.runner import has_module

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.toml"


class GuiAndSettingsTests(unittest.TestCase):
    def test_save_basic_settings_preserves_comments_and_validates_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config.toml"
            cfg_file.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

            save_basic_settings(
                cfg_file,
                "windows",
                watch_dir=r"D:\Incoming\ROMs",
                library_root=r"E:\Emulation\Library",
                post_process_action="delete",
            )

            raw_text = cfg_file.read_text(encoding="utf-8")
            # Existing comments must survive round-trip
            self.assertIn("QUICK START", raw_text)

            reloaded = load_config(cfg_file, platform_override="windows")
            self.assertEqual(reloaded.general.post_process_action, PostProcessAction.DELETE)
            self.assertEqual(reloaded.paths.watch_dir, Path(r"D:\Incoming\ROMs").resolve())
            self.assertEqual(reloaded.paths.raw_library_root, r"E:\Emulation\Library")

            with self.assertRaises(ConfigError):
                save_basic_settings(cfg_file, "windows", post_process_action="invalid_mode")

    def test_save_advanced_settings_and_semver_updater_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config.toml"
            cfg_file.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

            save_advanced_settings(
                cfg_file,
                general_updates={
                    "stability_poll_seconds": 1.5,
                    "stability_checks_required": 4,
                    "min_free_staging_gb": 10.0,
                },
                conversion_updates={
                    "psx_pbp_compression_level": 7,
                    "switch_merge_format": "nsp",
                    "wiiu_to_wua": False,
                },
            )
            reloaded = load_config(cfg_file, platform_override="windows")
            self.assertAlmostEqual(reloaded.general.stability_poll_seconds, 1.5)
            self.assertEqual(reloaded.general.stability_checks_required, 4)
            self.assertAlmostEqual(reloaded.general.min_free_staging_gb, 10.0)
            self.assertEqual(reloaded.conversion.psx_pbp_compression_level, 7)
            self.assertEqual(reloaded.conversion.switch_merge_format, "nsp")
            self.assertFalse(reloaded.conversion.wiiu_to_wua)

            # Invalid value must fail validation and leave live config untouched
            with self.assertRaises(ConfigError):
                save_advanced_settings(cfg_file, conversion_updates={"psx_pbp_compression_level": 99})
            still_ok = load_config(cfg_file, platform_override="windows")
            self.assertEqual(still_ok.conversion.psx_pbp_compression_level, 7)

            # SemVer comparison & non-git update refusal
            self.assertEqual(parse_semver("v0.3.1"), (0, 3, 1))
            self.assertTrue(is_newer_version("v0.3.0", "0.2.0"))
            self.assertTrue(is_newer_version("1.0.0", "0.9.9"))
            self.assertFalse(is_newer_version("v0.2.0", "0.2.0"))
            self.assertFalse(is_newer_version("0.1.9", "0.2.0"))

            res = perform_git_update(root)
            self.assertFalse(res.success)
            self.assertIn(".git", res.message)

            # Verify GitHub Atom feed parsing & automatic HTTP 403 rate-limit fallback
            sample_atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link rel="alternate" type="text/html" href="https://github.com/YoungLordAce/volundr/releases/tag/v0.3.0"/>
    <title>Völundr v0.3.0</title>
    <content type="html">&lt;h3&gt;Added&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Desktop GUI&lt;/li&gt;&lt;/ul&gt;</content>
  </entry>
</feed>"""
            parsed = parse_releases_atom_xml(sample_atom, current_version="0.2.0")
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertTrue(parsed.update_available)
            self.assertEqual(parsed.latest_version, "0.3.0")
            self.assertTrue(parsed.release_notes_is_html)
            self.assertIn("Desktop GUI", parsed.release_notes)

            import urllib.error

            clear_update_cache()
            try:
                rate_err = urllib.error.HTTPError(
                    "https://api.github.com/repos/YoungLordAce/volundr/releases/latest",
                    403,
                    "rate limit exceeded",
                    hdrs=None,
                    fp=None,
                )
                with (
                    patch("urllib.request.urlopen", side_effect=rate_err),
                    patch("src.gui.updater._check_via_atom_feed", return_value=parsed),
                ):
                    fallback_res = check_for_updates(current_version="0.2.0", bypass_cache=True)
                self.assertIsNone(fallback_res.error)
                self.assertTrue(fallback_res.update_available)
                self.assertEqual(fallback_res.latest_version, "0.3.0")
            finally:
                clear_update_cache()

    def test_cli_gui_command_prints_helpful_message_when_pyside6_missing(self) -> None:
        stderr_buf = io.StringIO()
        with patch("main._has_module", return_value=False), redirect_stderr(stderr_buf):
            rc = cli_main.main(["gui"])

        self.assertEqual(rc, 1)
        err_text = stderr_buf.getvalue()
        self.assertIn("PySide6", err_text)
        self.assertIn("requirements-gui.txt", err_text)

    @unittest.skipUnless(has_module("PySide6"), "Requires optional PySide6")
    def test_pyside6_main_window_offscreen_smoke(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from src.gui.app import VolundrMainWindow
        from src.gui.dialogs import AdvancedSettingsDialog, UpdateDialog
        from src.gui.setup_wizard import SetupWizardDialog, detect_tool_install_status
        from src.gui.theme import build_app_icon
        from src.gui.updater import UpdateCheckResult

        app = QApplication.instance() or QApplication([])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config.toml"
            cfg_file.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            lib_dir = root / "library"
            lib_dir.mkdir()
            save_basic_settings(
                cfg_file,
                "windows" if os.name == "nt" else "linux",
                watch_dir=str(root / "drop"),
                library_root=str(lib_dir),
                post_process_action="move",
            )
            cfg = load_config(cfg_file)

            icon_app = build_app_icon(watching=None)
            icon_on = build_app_icon(watching=True)
            icon_off = build_app_icon(watching=False)
            self.assertFalse(icon_app.isNull())
            self.assertFalse(icon_on.isNull())
            self.assertFalse(icon_off.isNull())
            self.assertGreaterEqual(len(icon_app.availableSizes()), 4)

            ini_file = root / "ui_prefs.ini"
            isolated_settings = QSettings(str(ini_file), QSettings.Format.IniFormat)

            # Patch background threads & isolate QSettings so tests never touch host prefs or network
            with (
                patch.object(VolundrMainWindow, "refresh_doctor", lambda self: None),
                patch.object(VolundrMainWindow, "_check_updates_silently", lambda self: None),
                patch("src.gui.app.QSettings", return_value=isolated_settings),
            ):
                # Default launch (`auto_watch=None`) must start Paused unless enabled in Settings
                win = VolundrMainWindow(cfg, auto_watch=None)
                try:
                    self.assertEqual(win._tabs.count(), 3)
                    self.assertEqual(win._edit_library_root.text(), str(lib_dir))
                    self.assertFalse(win._watching)
                    self.assertFalse(win._chk_auto_watch.isChecked())
                    self.assertEqual(win._status_indicator.text(), "Paused")

                    # Verify auto-watch & tray preferences persist cleanly across restarts
                    win._chk_auto_watch.setChecked(True)
                    self.assertTrue(
                        VolundrMainWindow._read_bool_setting(isolated_settings, "ui/auto_watch_on_launch", False)
                    )
                    win._chk_min_to_tray.setChecked(False)
                    self.assertFalse(
                        VolundrMainWindow._read_bool_setting(isolated_settings, "ui/minimize_to_tray", True)
                    )

                    # Verify outcome slot populates results table & activity log
                    outcome = RoutingOutcome(
                        source_path=root / "drop" / "Castlevania.zip",
                        routed_files=(lib_dir / "psx" / "Castlevania.chd",),
                        post_action_taken="moved source",
                    )
                    win._on_outcome_received(outcome)
                    self.assertEqual(win._results_table.rowCount(), 1)
                    self.assertEqual(win._results_table.item(0, 2).text(), "ROUTED")

                    # Verify doctor slot renders OK/WARN/FAIL rows cleanly
                    win._on_doctor_finished([
                        ("Runtime", "Python 3.11+", "OK", "3.14"),
                        ("Binaries", "DolphinTool", "WARN", "Not found"),
                    ])
                    self.assertEqual(win._doctor_table.rowCount(), 2)
                    self.assertIn("1 OK", win._doctor_summary.text())

                    # Verify AdvancedSettingsDialog constructs & saves cleanly
                    adv = AdvancedSettingsDialog(cfg, parent=win)
                    adv._spin_pbp_lvl.setValue(8)
                    adv._on_save()
                    self.assertEqual(load_config(cfg_file).conversion.psx_pbp_compression_level, 8)

                    # Verify UpdateDialog renders Markdown and uses clean "Update" button label
                    with patch.object(UpdateDialog, "start_check", lambda self: None):
                        upd = UpdateDialog(root, parent=win)
                        upd._on_check_done(
                            UpdateCheckResult(
                                current_version="0.2.0",
                                latest_version="0.2.0",
                                update_available=False,
                                release_url="https://github.com/YoungLordAce/volundr/releases/tag/v0.2.0",
                                release_title="v0.2.0",
                                release_notes="### Added\n- **Guided Setup** (`setup_volundr.py`)",
                            )
                        )
                        self.assertEqual(upd._btn_update.text(), "Update")
                        self.assertFalse(upd._btn_update.isEnabled())
                        html = upd._notes_view.toHtml()
                        self.assertIn("Guided Setup", html)
                        self.assertNotIn("### Added", html)
                        upd.close()

                    # Verify SetupWizardDialog constructs, inspects tool status, and navigates cleanly
                    tool_map = detect_tool_install_status(root, cfg.platform_key)
                    self.assertIn("7z", tool_map)
                    self.assertIn("wiiu", tool_map)
                    wiz = SetupWizardDialog(cfg_file, is_first_run=False, parent=win)
                    self.assertEqual(wiz._edit_library_root.text(), str(lib_dir))
                    self.assertEqual(wiz._stack.currentIndex(), 0)
                    wiz._go_next()
                    self.assertEqual(wiz._stack.currentIndex(), 1)
                    wiz._set_all_tool_checks(False)
                    self.assertEqual(wiz.selected_tool_ids(), [])
                    wiz.close()
                finally:
                    win._quitting = True
                    win._shutdown_cleanup()
                    win.close()
                    app.processEvents()


if __name__ == "__main__":
    unittest.main()
