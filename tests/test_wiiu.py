"""Unit and integration tests for Wii U identification, conversion/bundling, and quarantine safeguards."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.config_loader import (
    _check_seven_zip_rar_cached,
    check_seven_zip_supports_rar,
    load_config,
)
from src.identifier import identify_directory, identify_single_file
from src.models import ArchivePolicy, PostProcessAction
from src.router import ingest_target
import main as cli_main

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.example.toml"
DOCKER_CONFIG_PATH = PROJECT_ROOT / "config.docker.example.toml"


def _make_sample_meta_xml(
    title_id: str = "000500001019E600",
    title_version: int = 81,
    longname_en: str = "The Legend of Zelda:\nTwilight Princess HD",
) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<menu type="complex" access="777">\n'
        f'  <title_version type="unsignedInt" length="4">{title_version}</title_version>\n'
        f'  <title_id type="hexBinary" length="8">{title_id}</title_id>\n'
        f'  <longname_en type="string" length="512">{longname_en}</longname_en>\n'
        "</menu>\n"
    )


def _make_dummy_tmd(title_id_hex: str = "000500001019E600", version: int = 81) -> bytes:
    buf = bytearray(0x200)
    buf[0x18C:0x194] = bytes.fromhex(title_id_hex)
    buf[0x1DC:0x1DE] = version.to_bytes(2, "big")
    return bytes(buf)


class WiiUTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_config = load_config(CONFIG_PATH)

    def _make_isolated_config(
        self,
        root: Path,
        post_action: PostProcessAction = PostProcessAction.MOVE,
    ):
        drop = root / "drop"
        staging = root / ".staging"
        processed = root / "processed"
        quarantine = root / "quarantine"
        nas = root / "mock_nas_roms"
        for d in (drop, staging, processed, quarantine, nas):
            d.mkdir(parents=True, exist_ok=True)

        new_paths = replace(
            self.base_config.paths,
            watch_dir=drop,
            staging_dir=staging,
            processed_dir=processed,
            quarantine_dir=quarantine,
            library_root=nas,
            raw_library_root=str(nas),
        )
        new_general = replace(
            self.base_config.general,
            post_process_action=post_action,
            min_free_staging_gb=0.01,
        )
        new_bins = replace(
            self.base_config.binaries,
            zarchive=None,
            cdecrypt=None,
        )
        return replace(
            self.base_config,
            paths=new_paths,
            general=new_general,
            binaries=new_bins,
        )

    def test_single_file_wua_and_wux_identification_and_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)

            wua_src = cfg.paths.watch_dir / "Mario Kart 8 (USA) (axekin.com).wua"
            wua_src.write_bytes(b"WUA_PAYLOAD" * 16)
            wux_src = cfg.paths.watch_dir / "Pikmin 3 (Europe).wux"
            wux_src.write_bytes(b"WUX_PAYLOAD" * 16)

            self.assertEqual(identify_single_file(wua_src).system_id, "wiiu")
            self.assertEqual(identify_single_file(wux_src).system_id, "wiiu")

            out1 = ingest_target(wua_src, cfg, dry_run=False)
            out2 = ingest_target(wux_src, cfg, dry_run=False)

            self.assertEqual(out1.quarantined_files, ())
            self.assertEqual(out2.quarantined_files, ())
            self.assertEqual(
                out1.routed_files,
                (cfg.paths.library_root / "wiiu" / "Mario Kart 8 (USA).wua",),
            )
            self.assertEqual(
                out2.routed_files,
                (cfg.paths.library_root / "wiiu" / "Pikmin 3 (Europe).wux",),
            )
            self.assertTrue(out1.routed_files[0].is_file())
            self.assertTrue(out2.routed_files[0].is_file())

    def test_hybrid_loadiine_and_nus_archive_strips_encrypted_blobs_and_avoids_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)

            arc = cfg.paths.watch_dir / "THE LEGEND OF ZELDA Twilight Princess HD [Game]000500001019e600](axekin.com).zip"
            with zipfile.ZipFile(arc, "w") as zf:
                zf.writestr("code/Zelda.rpx", b"RPX_DATA" * 10)
                zf.writestr("code/cos.xml", "<app/>")
                zf.writestr("meta/meta.xml", _make_sample_meta_xml())
                zf.writestr("content/Z_bdor00.arc", b"ASSET_DATA" * 10)
                zf.writestr("content/empty_marker.dat", b"")
                # Redundant encrypted WUP/NUS blobs sitting at root
                zf.writestr("00000001.app", b"ENC_APP" * 50)
                zf.writestr("00000001.h3", b"HASH_DATA" * 10)
                zf.writestr("title.tmd", _make_dummy_tmd())
                zf.writestr("title.tik", b"TIK_DATA" * 8)
                zf.writestr("title.cert", b"CERT_DATA" * 8)

            dry_out = ingest_target(arc, cfg, dry_run=True)
            expected_dir = cfg.paths.library_root / "wiiu" / "The Legend of Zelda - Twilight Princess HD"
            self.assertEqual(dry_out.quarantined_files, ())
            self.assertEqual(dry_out.routed_files, (expected_dir,))
            self.assertTrue(arc.is_file())

            live_out = ingest_target(arc, cfg, dry_run=False)
            self.assertEqual(live_out.quarantined_files, ())
            self.assertEqual(live_out.routed_files, (expected_dir,))
            self.assertTrue((expected_dir / "code" / "Zelda.rpx").is_file())
            self.assertTrue((expected_dir / "meta" / "meta.xml").is_file())
            self.assertTrue((expected_dir / "content" / "Z_bdor00.arc").is_file())
            self.assertTrue((expected_dir / "content" / "empty_marker.dat").is_file())
            # Redundant encrypted blobs must NOT be copied to NAS or quarantine
            self.assertFalse((expected_dir / "00000001.app").exists())
            self.assertFalse((expected_dir / "title.tmd").exists())
            self.assertEqual(list(cfg.paths.quarantine_dir.iterdir()), [])
            self.assertTrue((cfg.paths.processed_dir / arc.name).is_file())

    def test_zarchive_wua_conversion_uses_titleid_version_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_zarchive = root / "zarchive.exe"
            fake_zarchive.write_bytes(b"FAKE_BIN")
            cfg = self._make_isolated_config(root)
            cfg = replace(
                cfg,
                binaries=replace(cfg.binaries, zarchive=fake_zarchive),
                conversion=replace(cfg.conversion, wiiu_to_wua=True),
            )

            arc = cfg.paths.watch_dir / "Twilight Princess HD.zip"
            with zipfile.ZipFile(arc, "w") as zf:
                zf.writestr("code/Zelda.rpx", b"RPX" * 10)
                zf.writestr("meta/meta.xml", _make_sample_meta_xml("000500001019E600", 81))
                zf.writestr("content/scene.dat", b"SCENE" * 10)

            observed_slots: list[str] = []

            def fake_subproc(cmd: list[str], status_label: str, cwd: Path | None = None) -> None:
                in_dir = Path(cmd[1])
                out_file = Path(cmd[2])
                slots = [p.name for p in in_dir.iterdir() if p.is_dir()]
                observed_slots.extend(slots)
                self.assertEqual(slots, ["000500001019e600_v81"])
                self.assertTrue((in_dir / slots[0] / "code" / "Zelda.rpx").is_file())
                self.assertTrue((in_dir / slots[0] / "content" / "scene.dat").is_file())
                out_file.write_bytes(b"ZARCHIVE_WUA_BYTES" * 16)

            with patch("src.converter._run_streaming_subprocess", side_effect=fake_subproc):
                dry_out = ingest_target(arc, cfg, dry_run=True)
                expected_wua = cfg.paths.library_root / "wiiu" / "The Legend of Zelda - Twilight Princess HD.wua"
                self.assertEqual(dry_out.routed_files, (expected_wua,))

                live_out = ingest_target(arc, cfg, dry_run=False)
                self.assertEqual(live_out.routed_files, (expected_wua,))
                self.assertTrue(expected_wua.is_file())
                self.assertEqual(observed_slots, ["000500001019e600_v81"])

    def test_encrypted_only_nus_archive_without_cdecrypt_quarantines_whole_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root, post_action=PostProcessAction.DELETE)

            arc = cfg.paths.watch_dir / "Xenoblade X [NUS].zip"
            with zipfile.ZipFile(arc, "w") as zf:
                zf.writestr("title.tmd", _make_dummy_tmd("00050000101C4D00", 48))
                zf.writestr("title.tik", b"TIK" * 16)
                zf.writestr("00000001.app", b"APP1" * 32)
                zf.writestr("00000001.h3", b"H3_1" * 16)

            out = ingest_target(arc, cfg, dry_run=False)
            self.assertEqual(out.routed_files, ())
            self.assertEqual(out.quarantined_files, (cfg.paths.quarantine_dir / arc.name,))
            self.assertTrue((cfg.paths.quarantine_dir / arc.name).is_file())
            self.assertFalse((cfg.paths.quarantine_dir / "00000001.app").exists())
            self.assertFalse((cfg.paths.quarantine_dir / "title.tmd").exists())
            # DELETE override safety: quarantined archive source is preserved in processed/
            self.assertTrue((cfg.paths.processed_dir / arc.name).is_file())

    def test_unidentified_multifile_archive_quarantines_intact_archive_not_loose_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)

            arc = cfg.paths.watch_dir / "Unknown_Multifile_Dump.zip"
            with zipfile.ZipFile(arc, "w") as zf:
                for i in range(15):
                    zf.writestr(f"assets/file_{i:02d}.dat", b"UNKNOWN_PAYLOAD" * 8)

            out = ingest_target(arc, cfg, dry_run=False)
            self.assertEqual(out.routed_files, ())
            self.assertEqual(out.quarantined_files, (cfg.paths.quarantine_dir / arc.name,))
            self.assertEqual(
                [p.name for p in cfg.paths.quarantine_dir.iterdir()],
                [arc.name],
            )
            self.assertTrue((cfg.paths.processed_dir / arc.name).is_file())

    def test_docker_config_has_wiiu_parity(self) -> None:
        docker_cfg = load_config(DOCKER_CONFIG_PATH, platform_override="linux")
        self.assertIn("wiiu", docker_cfg.systems)
        self.assertEqual(docker_cfg.systems["wiiu"].folder, "wiiu")
        self.assertEqual(docker_cfg.systems["wiiu"].archive_policy, ArchivePolicy.EXTRACT)
        self.assertTrue(docker_cfg.conversion.wiiu_to_wua)
        self.assertEqual(set(docker_cfg.systems.keys()), set(self.base_config.systems.keys()))

    def test_doctor_is_readonly_and_process_respects_active_staging_locks(self) -> None:
        from src.archive_handler import StagingJobDir
        from src.watcher import purge_stale_staging

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)

            stale_job_1 = cfg.paths.staging_dir / "job_deadbeef0001"
            stale_job_1.mkdir()
            (stale_job_1 / "orphan.bin").write_bytes(b"ORPHAN")

            captured_checks: list[tuple[str, str, str, str]] = []
            with patch(
                "main._render_doctor_report",
                side_effect=lambda _cfg, checks: captured_checks.extend(checks),
            ):
                rc = cli_main.run_doctor(cfg)

            self.assertEqual(rc, 0)
            # `doctor` must be strictly read-only so `docker exec ... doctor` never wipes active `.staging/` jobs
            self.assertTrue(stale_job_1.exists())
            check_names = {item for _, item, _, _ in captured_checks}
            self.assertIn("pycryptodome", check_names)
            self.assertIn("pyctr", check_names)
            self.assertIn("zarchive (Wii U .wua)", check_names)
            self.assertIn("cdecrypt (Wii U NUS)", check_names)

            # Dry-run process should NOT purge staging
            cli_main.run_process([], cfg, dry_run=True)
            self.assertTrue(stale_job_1.exists())

            # Active StagingJobDir must be protected while truly stale siblings are purged
            with StagingJobDir(cfg, create=True) as active_job:
                (active_job / "converting.iso").write_bytes(b"ACTIVE_JOB")
                cli_main.run_process([], cfg, dry_run=False)
                self.assertTrue(active_job.exists())
                self.assertFalse(stale_job_1.exists())

            # Stale directory left after exit is purged cleanly
            stale_job_2 = cfg.paths.staging_dir / "job_deadbeef0002"
            stale_job_2.mkdir()
            (stale_job_2 / "orphan2.bin").write_bytes(b"ORPHAN2")
            self.assertEqual(purge_stale_staging(cfg), 1)
            self.assertFalse(stale_job_2.exists())

    def test_dropped_wiiu_loadiine_folder_with_zero_byte_file_and_nus_folder_pass_watch_stability(self) -> None:
        from src.watcher import probe_target_snapshot, scan_watch_dir_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)
            cfg = replace(
                cfg,
                general=replace(
                    cfg.general,
                    stability_poll_seconds=0.02,
                    stability_checks_required=2,
                ),
            )

            # 1. Dropped unpacked Loadiine folder containing a legitimate 0-byte marker file
            game_dir = cfg.paths.watch_dir / "Twilight Princess HD"
            (game_dir / "code").mkdir(parents=True)
            (game_dir / "meta").mkdir(parents=True)
            (game_dir / "content").mkdir(parents=True)
            (game_dir / "code" / "Zelda.rpx").write_bytes(b"RPX_DATA" * 32)
            (game_dir / "meta" / "meta.xml").write_text(_make_sample_meta_xml(), encoding="utf-8")
            (game_dir / "content" / "empty_marker.dat").write_bytes(b"")

            ready_l, sig_l = probe_target_snapshot(game_dir)
            self.assertTrue(ready_l)
            self.assertGreater(sig_l[1], 0)

            # 2. Dropped encrypted WUP/NUS folder (`title.tmd` + `title.tik` + `*.app`)
            nus_dir = cfg.paths.watch_dir / "Xenoblade_NUS_Drop"
            nus_dir.mkdir()
            (nus_dir / "title.tmd").write_bytes(_make_dummy_tmd("00050000101C4D00", 48))
            (nus_dir / "title.tik").write_bytes(b"TIK" * 16)
            (nus_dir / "00000001.app").write_bytes(b"APP" * 32)

            ready_n, sig_n = probe_target_snapshot(nus_dir)
            self.assertTrue(ready_n)
            self.assertGreater(sig_n[1], 0)

            outcomes, errs = scan_watch_dir_once(cfg, dry_run=False, verify_stability=True)
            self.assertEqual(errs, 0)
            self.assertEqual(len(outcomes), 2)
            expected_loadiine = cfg.paths.library_root / "wiiu" / "The Legend of Zelda - Twilight Princess HD"
            self.assertTrue((expected_loadiine / "code" / "Zelda.rpx").is_file())
            self.assertTrue((expected_loadiine / "content" / "empty_marker.dat").is_file())

    def test_check_seven_zip_supports_rar_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_7z = Path(tmp) / "7zz.exe"
            fake_7z.write_bytes(b"FAKE_7ZZ")
            _check_seven_zip_rar_cached.cache_clear()

            class _FakeCompleted:
                returncode = 0
                stdout = "Formats:\n  Rar  rar r00\n"
                stderr = ""

            with patch("src.config_loader.run_capture", return_value=_FakeCompleted()) as mock_run:
                self.assertTrue(check_seven_zip_supports_rar(fake_7z))
                self.assertTrue(check_seven_zip_supports_rar(fake_7z))
                self.assertEqual(mock_run.call_count, 1)

    def test_wiiu_inspect_planned_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_isolated_config(root)
            fake_zarchive = root / "zarchive.exe"
            fake_zarchive.write_bytes(b"FAKE")
            fake_cdecrypt = root / "cdecrypt.exe"
            fake_cdecrypt.write_bytes(b"FAKE")

            # 1. Loadiine tree -> zarchive pack (.wua) vs clean folder fallback
            game_dir = root / "Twilight_Princess_HD"
            (game_dir / "code").mkdir(parents=True)
            (game_dir / "meta").mkdir(parents=True)
            (game_dir / "content").mkdir(parents=True)
            (game_dir / "code" / "Zelda.rpx").write_bytes(b"RPX_DATA" * 10)
            (game_dir / "meta" / "meta.xml").write_text(_make_sample_meta_xml(), encoding="utf-8")
            (game_dir / "content" / "scene.dat").write_bytes(b"SCENE" * 10)
            loadiine_res = identify_directory(game_dir)[0]

            cfg_with_zarchive = replace(
                cfg,
                binaries=replace(cfg.binaries, zarchive=fake_zarchive),
                conversion=replace(cfg.conversion, wiiu_to_wua=True),
            )
            action_wua = cli_main._describe_planned_action(loadiine_res, cfg_with_zarchive, from_archive=True)
            self.assertIn("Extract -> zarchive pack (", action_wua)
            self.assertIn(".wua) ->", action_wua)

            cfg_folder_only = replace(
                cfg,
                conversion=replace(cfg.conversion, wiiu_to_wua=False),
            )
            action_folder = cli_main._describe_planned_action(loadiine_res, cfg_folder_only, from_archive=False)
            self.assertIn("Route clean Loadiine folder (", action_folder)

            # 2. Encrypted WUP/NUS without cdecrypt vs with cdecrypt
            nus_dir = root / "Encrypted_NUS"
            nus_dir.mkdir()
            (nus_dir / "title.tmd").write_bytes(_make_dummy_tmd())
            (nus_dir / "title.tik").write_bytes(b"T" * 64)
            (nus_dir / "00000000.app").write_bytes(b"A" * 128)
            nus_res = identify_directory(nus_dir)[0]

            cfg_no_cdecrypt = replace(cfg, binaries=replace(cfg.binaries, cdecrypt=None))
            action_quarantine = cli_main._describe_planned_action(nus_res, cfg_no_cdecrypt, from_archive=False)
            self.assertIn("Quarantine (Encrypted Wii U WUP/NUS", action_quarantine)

            cfg_with_cdecrypt = replace(
                cfg_with_zarchive,
                binaries=replace(cfg_with_zarchive.binaries, cdecrypt=fake_cdecrypt),
            )
            action_cdecrypt = cli_main._describe_planned_action(nus_res, cfg_with_cdecrypt, from_archive=True)
            self.assertIn("Extract -> cdecrypt NUS -> zarchive pack (", action_cdecrypt)

            # 3. Standalone .wua file -> Route .wua as-is
            wua_file = root / "Wind Waker HD.wua"
            wua_file.write_bytes(b"WUA_PAYLOAD")
            wua_res = identify_single_file(wua_file)
            action_passthrough = cli_main._describe_planned_action(wua_res, cfg_with_zarchive, from_archive=False)
            self.assertIn("Route .wua as-is ->", action_passthrough)


if __name__ == "__main__":
    unittest.main()
