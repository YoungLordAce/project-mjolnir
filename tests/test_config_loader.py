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


if __name__ == "__main__":
    unittest.main()
