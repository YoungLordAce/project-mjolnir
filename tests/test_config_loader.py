"""Unit tests for example config templates and friendly missing-config UX."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

import main as cli_main
from src.config_loader import ConfigError, MissingConfigError, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.toml"
DOCKER_EXAMPLE_CONFIG = PROJECT_ROOT / "config.docker.example.toml"


class ConfigLoaderUXTests(unittest.TestCase):
    def test_example_configs_load_with_full_parity(self) -> None:
        win_cfg = load_config(EXAMPLE_CONFIG, platform_override="windows")
        lin_cfg = load_config(EXAMPLE_CONFIG, platform_override="linux")
        doc_cfg = load_config(DOCKER_EXAMPLE_CONFIG, platform_override="linux")

        self.assertEqual(set(win_cfg.systems.keys()), set(lin_cfg.systems.keys()))
        self.assertEqual(set(win_cfg.systems.keys()), set(doc_cfg.systems.keys()))
        self.assertEqual(win_cfg.conversion, doc_cfg.conversion)
        self.assertEqual(doc_cfg.paths.raw_library_root, "/library")

    def test_missing_config_raises_helpful_setup_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.example.toml").write_text("# example\n", encoding="utf-8")
            missing = root / "config.toml"

            with self.assertRaises(MissingConfigError) as ctx:
                load_config(missing)

            self.assertIsInstance(ctx.exception, ConfigError)
            msg = str(ctx.exception)
            self.assertIn("config.example.toml", msg)
            self.assertIn("Copy-Item config.example.toml config.toml", msg)
            self.assertIn("cp config.example.toml config.toml", msg)
            self.assertIn("library_root", msg)
            self.assertIn("python main.py doctor", msg)

    def test_cli_missing_config_exits_gracefully_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.example.toml").write_text("# example\n", encoding="utf-8")
            missing = root / "config.toml"

            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                rc = cli_main.main(["--config", str(missing), "doctor"])

            self.assertEqual(rc, 2)
            err_text = stderr_buf.getvalue()
            self.assertIn("config.example.toml", err_text)
            self.assertIn("python main.py doctor", err_text)
            self.assertNotIn("Traceback", err_text)

    def test_switch_prod_keys_auto_discovered_and_synced_to_keys_txt(self) -> None:
        import os
        from src.converter import ensure_squirrel_keys_txt

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config.toml"
            cfg_file.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

            ztools = root / "tools" / "nscb" / "ztools"
            ztools.mkdir(parents=True)
            fake_sq = ztools / "squirrel.exe"
            fake_sq.write_bytes(b"FAKE_SQ")
            prod_keys = ztools / "prod.keys"
            prod_keys.write_text("header_key = 00\n", encoding="utf-8")

            cfg = load_config(cfg_file, platform_override="windows")
            self.assertEqual(cfg.binaries.squirrel, fake_sq.resolve())
            self.assertEqual(cfg.binaries.squirrel_keys, prod_keys.resolve())

            synced = ensure_squirrel_keys_txt(cfg)
            self.assertEqual(synced, ztools.resolve() / "keys.txt")
            self.assertTrue(synced.is_file())
            self.assertTrue(prod_keys.is_file())
            self.assertEqual(synced.read_text(encoding="utf-8"), "header_key = 00\n")

            # Updating prod.keys with a newer mtime refreshes keys.txt automatically
            prod_keys.write_text("header_key = 11\n", encoding="utf-8")
            future_ns = synced.stat().st_mtime_ns + 5_000_000_000
            os.utime(prod_keys, ns=(future_ns, future_ns))

            cfg_after = load_config(cfg_file, platform_override="windows")
            synced2 = ensure_squirrel_keys_txt(cfg_after)
            self.assertEqual(synced2.read_text(encoding="utf-8"), "header_key = 11\n")

    def test_render_routing_outcome_preserves_bracketed_version_tags(self) -> None:
        from contextlib import redirect_stdout
        from src.models import RoutingOutcome

        outcome = RoutingOutcome(
            source_path=Path("drop/Broforce.rar"),
            routed_files=(Path("roms/switch/Broforce [010060A00B53C000] [v262144] (1G+1U).xci"),),
            post_action_taken="moved source -> processed/Broforce [v262144].rar",
        )
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            cli_main._render_routing_outcome(outcome)
        rendered = out_buf.getvalue()
        self.assertIn("[v262144]", rendered)


if __name__ == "__main__":
    unittest.main()
