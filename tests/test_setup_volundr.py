"""Unit tests for the optional guided setup script (`setup_volundr.py`)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

import setup_volundr as sm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.toml"


class SetupVolundrTests(unittest.TestCase):
    def test_update_library_root_in_toml_windows_and_linux(self) -> None:
        template = EXAMPLE_CONFIG.read_text(encoding="utf-8")

        win_updated = sm.update_library_root_in_toml(template, "windows", r"D:\My ROMs\Library")
        parsed_win = tomllib.loads(win_updated)
        self.assertEqual(parsed_win["paths"]["windows"]["library_root"], r"D:\My ROMs\Library")
        self.assertEqual(parsed_win["paths"]["linux"]["library_root"], "/mnt/nas/games/library/roms")

        lin_updated = sm.update_library_root_in_toml(
            template, "linux", "smb://my-nas.local/share/roms"
        )
        parsed_lin = tomllib.loads(lin_updated)
        self.assertEqual(
            parsed_lin["paths"]["linux"]["library_root"], "smb://my-nas.local/share/roms"
        )
        self.assertEqual(parsed_lin["paths"]["windows"]["library_root"], r"Z:\games\library\roms")

    def test_setup_config_file_preserves_existing_and_supports_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.example.toml").write_text(
                EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
            )

            # Dry run must not write config.toml
            sm.setup_config_file(
                project_root=root,
                platform_key="windows",
                library_root=r"E:\Games\ROMs",
                interactive=False,
                dry_run=True,
            )
            self.assertFalse((root / "config.toml").exists())

            # Real run writes valid config.toml
            cfg_path = sm.setup_config_file(
                project_root=root,
                platform_key="windows",
                library_root=r"E:\Games\ROMs",
                interactive=False,
                dry_run=False,
            )
            self.assertTrue(cfg_path.is_file())
            parsed = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["paths"]["windows"]["library_root"], r"E:\Games\ROMs")

            # Existing config.toml must never be overwritten
            cfg_path.write_text("# custom personal config\n", encoding="utf-8")
            sm.setup_config_file(
                project_root=root,
                platform_key="windows",
                library_root=r"F:\ShouldNotOverwrite",
                interactive=False,
                dry_run=False,
            )
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), "# custom personal config\n")

    def test_pick_github_release_asset_excludes_gui_and_matches_cli(self) -> None:
        release = {
            "html_url": "https://github.com/RupertAvery/PSXPackager/releases/tag/v1.8.0",
            "assets": [
                {
                    "name": "PSXPackagerGUI-win-x64.zip",
                    "browser_download_url": "https://example.invalid/gui.zip",
                },
                {
                    "name": "psxpackager-win-x64.zip",
                    "browser_download_url": "https://example.invalid/cli.zip",
                },
            ],
        }
        asset = sm.pick_github_release_asset(
            release,
            (r"psxpackager.*win.*x64.*\.zip$",),
            exclude_patterns=(r"gui",),
        )
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertEqual(asset.name, "psxpackager-win-x64.zip")
        self.assertEqual(asset.url, "https://example.invalid/cli.zip")

        # Verify `pkg2zip.zip` (lusid1/pkg2zip Windows asset) matches cleanly
        pkg2zip_release = {
            "html_url": "https://github.com/lusid1/pkg2zip/releases/tag/v2.3",
            "assets": [
                {"name": "pkg2zip.zip", "browser_download_url": "https://example.invalid/pkg2zip.zip"},
            ],
        }
        p2z_asset = sm.pick_github_release_asset(
            pkg2zip_release,
            (r"pkg2zip.*win64.*\.zip$", r"^pkg2zip\.zip$", r"pkg2zip.*\.zip$"),
            exclude_patterns=(r"linux", r"mac", r"darwin", r"arm", r"source"),
        )
        self.assertIsNotNone(p2z_asset)
        assert p2z_asset is not None
        self.assertEqual(p2z_asset.name, "pkg2zip.zip")

        # Verify Dolphin JSON update API constructs the official x64 .7z URL
        with patch.object(sm, "_fetch_json", return_value={"shortrev": "2609b"}):
            self.assertEqual(
                sm._find_latest_dolphin_win_url(),
                "https://dl.dolphin-emu.org/releases/2609b/dolphin-2609b-x64.7z",
            )

    def test_safe_extract_zip_blocks_traversal_and_extracts_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good_zip = root / "good.zip"
            with zipfile.ZipFile(good_zip, "w") as zf:
                zf.writestr("pkg/zarchive.exe", b"MZ_FAKE")

            out_good = root / "out_good"
            sm._safe_extract_zip(good_zip, out_good)
            self.assertTrue((out_good / "pkg" / "zarchive.exe").is_file())

            bad_zip = root / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../escape.exe", b"EVIL")

            with self.assertRaises(ValueError):
                sm._safe_extract_zip(bad_zip, root / "out_bad")

    def test_install_selected_tools_skips_present_and_handles_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Pre-populate a fake chdman.exe so it should be skipped
            fake_chdman = root / "tools" / "chdman" / "chdman.exe"
            fake_chdman.parent.mkdir(parents=True)
            fake_chdman.write_bytes(b"FAKE_CHDMAN")

            results = sm.install_selected_tools(
                ["chdman", "psxpackager", "wiiu", "psvita"],
                project_root=root,
                platform_key="windows",
                arch="x64",
                force=False,
                dry_run=True,
            )
            by_id = {r.tool_id: r.status for r in results}
            self.assertEqual(by_id["chdman"], "skipped")
            self.assertEqual(by_id["psxpackager"], "dry-run")
            self.assertEqual(by_id["wiiu/zarchive"], "dry-run")
            self.assertEqual(by_id["wiiu/cdecrypt"], "dry-run")
            self.assertEqual(by_id["psvita/pkg2zip"], "dry-run")


if __name__ == "__main__":
    unittest.main()
