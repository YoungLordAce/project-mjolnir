"""Automated unit & live Watchdog integration tests for Phase 4 (`watcher.py`, `scan`, `watch`)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from src.config_loader import load_config
from src.models import PostProcessAction, RoutingOutcome
from src.watcher import (
    PipelineWatcher,
    can_open_exclusively,
    is_temp_download_file,
    scan_watch_dir_once,
)
from tests.test_phase2 import _build_iso9660_image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.example.toml"


class Phase4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_config = load_config(CONFIG_PATH)

    def _make_fast_test_config(self, root: Path):
        drop = root / "drop"
        staging = root / ".staging"
        processed = root / "processed"
        quarantine = root / "quarantine"
        nas = root / "mock_nas_roms"
        for d in (drop, staging, processed, quarantine, nas):
            d.mkdir(parents=True, exist_ok=True)
        (drop / ".gitkeep").write_bytes(b"")

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
            post_process_action=PostProcessAction.MOVE,
            stability_poll_seconds=0.06,
            stability_checks_required=3,
            min_free_staging_gb=0.01,
        )
        return replace(self.base_config, paths=new_paths, general=new_general)

    def test_exclusive_file_lock_and_temp_filters(self) -> None:
        self.assertTrue(is_temp_download_file(Path("Game.7z.crdownload")))
        self.assertTrue(is_temp_download_file(Path("Game.zip.part")))
        self.assertTrue(is_temp_download_file(Path("~syncthing~Game.chd.tmp")))
        self.assertFalse(is_temp_download_file(Path("Game.7z")))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "downloading.iso"
            fh = target.open("wb")
            try:
                fh.write(b"\x00" * 4096)
                fh.flush()
                if sys.platform != "win32":
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # On Windows, an open handle blocks Win32 `CreateFileW(dwShareMode=0)`;
                # on Linux/POSIX, an active `flock(LOCK_EX)` blocks `flock(LOCK_EX | LOCK_NB)`.
                self.assertFalse(can_open_exclusively(target))
            finally:
                fh.close()
            self.assertTrue(can_open_exclusively(target))

    def test_live_watchdog_browser_crdownload_rename_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            completed_evt = threading.Event()
            outcomes: list[RoutingOutcome] = []
            errors: list[Exception] = []

            def _on_outcome(out: RoutingOutcome) -> None:
                outcomes.append(out)
                completed_evt.set()

            def _on_err(_p: Path, exc: Exception) -> None:
                errors.append(exc)
                completed_evt.set()

            watcher = PipelineWatcher(cfg, on_outcome=_on_outcome, on_error=_on_err)
            watcher.start_background(scan_existing=True)
            try:
                # 1. Simulate Chrome writing `.crdownload` first
                temp_dl = cfg.paths.watch_dir / "MetroidZeroMission.zip.crdownload"
                final_dl = cfg.paths.watch_dir / "MetroidZeroMission.zip"
                with zipfile.ZipFile(temp_dl, "w") as zf:
                    zf.writestr("Metroid Zero Mission (USA).gba", b"GBA_ROM_DATA" * 256)

                time.sleep(0.25)
                # Must NOT have processed `.crdownload`
                self.assertEqual(len(outcomes), 0)
                self.assertTrue(temp_dl.exists())

                # 2. Browser finishes download -> renames `.crdownload` to `.zip` (`on_moved`)
                temp_dl.replace(final_dl)

                finished = completed_evt.wait(timeout=8.0)
                self.assertTrue(finished, "Timed out waiting for PipelineWatcher to ingest renamed zip")
                self.assertEqual(errors, [])
                self.assertEqual(len(outcomes), 1)
                self.assertTrue(
                    (cfg.paths.library_root / "gba" / "Metroid Zero Mission (USA).gba").is_file()
                )
                self.assertTrue((cfg.paths.processed_dir / "MetroidZeroMission.zip").is_file())
                self.assertFalse(final_dl.exists())
            finally:
                watcher.stop(timeout=5.0)

    def test_scan_loose_cue_bin_and_dropped_folder_with_orphan_cleanup(self) -> None:
        chdman = self.base_config.binaries.chdman
        if not chdman:
            self.skipTest("Requires chdman")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            drop = cfg.paths.watch_dir

            # 1. Loose top-level `.cue` + `.bin` directly in `drop/`
            bin_f = drop / "Wipeout.bin"
            cue_f = drop / "Wipeout.cue"
            bin_f.write_bytes(
                _build_iso9660_image(
                    sector_size=2352,
                    data_offset=24,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SCUS_943.01;1\r\n"},
                    total_sectors=48,
                )
            )
            cue_f.write_text(
                'FILE "Wipeout.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
                encoding="utf-8",
            )

            # 2. Dropped subfolder containing a ROM
            sub = drop / "NDS_Drop_Folder"
            sub.mkdir()
            (sub / "MarioKartDS.nds").write_bytes(b"NDS_CART_PAYLOAD" * 256)

            # 3. Random non-ROM `.txt` note in `drop/` (must be peacefully ignored)
            note = drop / "notes.txt"
            note.write_text("keep me untouched", encoding="utf-8")

            outcomes, err_count = scan_watch_dir_once(cfg, dry_run=False, verify_stability=True)
            self.assertEqual(err_count, 0)
            self.assertEqual(len(outcomes), 2)

            # Verify NAS outputs
            self.assertTrue((cfg.paths.library_root / "psx" / "Wipeout.chd").is_file())
            self.assertTrue((cfg.paths.library_root / "nds" / "MarioKartDS.nds").is_file())

            # Verify BOTH Wipeout.cue AND Wipeout.bin moved to `processed/` (zero orphan `.bin` in drop!)
            self.assertFalse(cue_f.exists())
            self.assertFalse(bin_f.exists())
            self.assertTrue((cfg.paths.processed_dir / "Wipeout.cue").is_file())
            self.assertTrue((cfg.paths.processed_dir / "Wipeout.bin").is_file())
            self.assertTrue((cfg.paths.processed_dir / "NDS_Drop_Folder").is_dir())

            # Verify `.gitkeep` and `notes.txt` were peacefully left alone in `drop/`
            self.assertTrue((drop / ".gitkeep").exists())
            self.assertTrue(note.exists())

    def test_romarr_platform_bucket_subdirs_processed_individually(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            drop = cfg.paths.watch_dir

            # Simulate ROMarr writing multiple games into `drop/gba/` and `drop/nds/`
            gba_bucket = drop / "gba"
            nds_bucket = drop / "nds"
            gba_bucket.mkdir()
            nds_bucket.mkdir()
            g1 = gba_bucket / "Golden Sun (USA).gba"
            g2 = gba_bucket / "Metroid Fusion (USA).gba"
            n1 = nds_bucket / "Chrono Trigger (USA).nds"
            g1.write_bytes(b"GBA_1" * 128)
            g2.write_bytes(b"GBA_2" * 128)
            n1.write_bytes(b"NDS_1" * 128)

            outcomes, err_count = scan_watch_dir_once(cfg, dry_run=False, verify_stability=True)
            self.assertEqual(err_count, 0)
            self.assertEqual(len(outcomes), 3)
            self.assertTrue((cfg.paths.library_root / "gba" / "Golden Sun (USA).gba").is_file())
            self.assertTrue((cfg.paths.library_root / "gba" / "Metroid Fusion (USA).gba").is_file())
            self.assertTrue((cfg.paths.library_root / "nds" / "Chrono Trigger (USA).nds").is_file())

            # Bucket folders themselves stay intact for subsequent ROMarr imports
            self.assertTrue(gba_bucket.is_dir())
            self.assertTrue(nds_bucket.is_dir())
            self.assertFalse(g1.exists())
            self.assertFalse(g2.exists())
            self.assertFalse(n1.exists())

    def test_gamarr_sidecar_ignored_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            drop = cfg.paths.watch_dir

            bucket = drop / "gba"
            bucket.mkdir()
            rom = bucket / "Advance Wars (USA).gba"
            rom.write_bytes(b"GBA_DATA" * 128)
            sidecar = bucket / "Advance Wars (USA).gba.gamarr.json"
            sidecar.write_text('{"title": "Advance Wars"}', encoding="utf-8")

            outcomes, err_count = scan_watch_dir_once(cfg, dry_run=False, verify_stability=True)
            self.assertEqual(err_count, 0)
            self.assertEqual(len(outcomes), 1)
            self.assertTrue((cfg.paths.library_root / "gba" / "Advance Wars (USA).gba").is_file())
            self.assertFalse(rom.exists())
            self.assertFalse(sidecar.exists())
            self.assertTrue((cfg.paths.processed_dir / "Advance Wars (USA).gba.gamarr.json").is_file())

    def test_staging_job_dir_cleanup_and_watcher_bucket_registration(self) -> None:
        from src.archive_handler import StagingJobDir

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            drop = cfg.paths.watch_dir

            # 1. StagingJobDir guarantees directory creation and automatic cleanup on exception
            allocated_dir: Path | None = None
            with self.assertRaises(RuntimeError):
                with StagingJobDir(cfg, create=True) as job_dir:
                    allocated_dir = job_dir
                    self.assertTrue(job_dir.is_dir())
                    (job_dir / "temp.bin").write_bytes(b"PAYLOAD")
                    raise RuntimeError("simulated conversion crash")
            self.assertIsNotNone(allocated_dir)
            self.assertFalse(allocated_dir.exists())

            # 2. PipelineWatcher.register_candidate expands ROMarr platform buckets and claims .bin -> .cue
            psx_bucket = drop / "psx"
            psx_bucket.mkdir()
            bin_f = psx_bucket / "Castlevania.bin"
            cue_f = psx_bucket / "Castlevania.cue"
            bin_f.write_bytes(b"\x00" * 2352)
            cue_f.write_text(
                'FILE "Castlevania.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
                encoding="utf-8",
            )

            watcher = PipelineWatcher(cfg)
            watcher.register_candidate(psx_bucket)
            self.assertIn(cue_f.resolve(), watcher._pending_stability)
            self.assertNotIn(bin_f.resolve(), watcher._pending_stability)
            self.assertNotIn(psx_bucket.resolve(), watcher._pending_stability)

    def test_poller_exception_resilience_and_transient_retry_backoff(self) -> None:
        from src.converter import ConversionError
        from src.router import RoutingError, execute_ingestion_batch as real_exec

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            cfg = replace(
                cfg,
                general=replace(
                    cfg.general,
                    stability_poll_seconds=0.03,
                    stability_checks_required=1,
                ),
            )
            drop = cfg.paths.watch_dir

            # 1. Poller survives an injected exception and processes subsequent drops
            done_evt = threading.Event()
            outcomes: list[RoutingOutcome] = []
            errors: list[tuple[Path, Exception]] = []

            def _on_out(out: RoutingOutcome) -> None:
                outcomes.append(out)
                done_evt.set()

            def _on_err(p: Path, exc: Exception) -> None:
                errors.append((p, exc))

            watcher = PipelineWatcher(cfg, on_outcome=_on_out, on_error=_on_err)
            watcher._retry_backoffs = (0.05, 0.1)

            orig_poll_once = watcher._poll_stability_once
            poll_calls = {"n": 0}

            def _flaky_poll_once(watch_root: Path, checks_needed: int) -> None:
                poll_calls["n"] += 1
                if poll_calls["n"] == 1:
                    raise RuntimeError("simulated poller hiccup")
                orig_poll_once(watch_root, checks_needed)

            # Inject a transient RoutingError on attempt 1 of `execute_ingestion_batch`, then succeed on attempt 2
            exec_calls = {"n": 0}

            def _flaky_exec(b_kind: str, batch: list[Path], config, dry_run: bool = False):
                exec_calls["n"] += 1
                if exec_calls["n"] == 1:
                    raise RoutingError("transient NAS disconnect")
                return real_exec(b_kind, batch, config, dry_run=dry_run)

            watcher._poll_stability_once = _flaky_poll_once  # type: ignore[method-assign]
            with patch("src.watcher.execute_ingestion_batch", side_effect=_flaky_exec):
                watcher.start_background(scan_existing=False)
                try:
                    rom = drop / "RetryTest.gba"
                    rom.write_bytes(b"GBA_RETRY_PAYLOAD" * 64)
                    watcher.register_candidate(rom)

                    self.assertTrue(
                        done_evt.wait(timeout=6.0),
                        "Poller or transient retry failed to recover and ingest RetryTest.gba",
                    )
                    self.assertTrue(watcher._poller_thread is not None and watcher._poller_thread.is_alive())
                    self.assertEqual(exec_calls["n"], 2)
                    self.assertEqual(len(outcomes), 1)
                    self.assertTrue((cfg.paths.library_root / "gba" / "RetryTest.gba").is_file())
                    self.assertTrue(any(isinstance(e, RuntimeError) for _, e in errors))
                    self.assertTrue(any(isinstance(e, RoutingError) for _, e in errors))

                    # 2. Permanent ConversionError must NOT retry
                    perm_err_evt = threading.Event()
                    perm_calls = {"n": 0}

                    def _perm_fail(b_kind: str, batch: list[Path], config, dry_run: bool = False):
                        perm_calls["n"] += 1
                        perm_err_evt.set()
                        raise ConversionError("corrupt ROM")

                    with patch("src.watcher.execute_ingestion_batch", side_effect=_perm_fail):
                        bad_rom = drop / "Corrupt.gba"
                        bad_rom.write_bytes(b"BAD_ROM" * 32)
                        watcher.register_candidate(bad_rom)
                        self.assertTrue(perm_err_evt.wait(timeout=4.0))
                        time.sleep(0.25)
                        self.assertEqual(perm_calls["n"], 1)
                finally:
                    watcher.stop(timeout=5.0)

    def test_orphan_switch_nas_recheck_throttling_and_cleanup(self) -> None:
        from src.switch_bundler import find_nas_switch_family_files as real_find_nas

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            upd_nsp = drop / "OrphanGame [0100BBB000000800][v65536].nsp"
            upd_nsp.write_bytes(b"PFS0" * 64)

            watcher = PipelineWatcher(cfg)
            watcher._orphan_nas_recheck_seconds = 60.0
            watcher._ready_buffer.append(upd_nsp.resolve())
            watcher._ready_signatures[upd_nsp.resolve()] = (1, upd_nsp.stat().st_size)

            with patch("src.watcher.find_nas_switch_family_files", wraps=real_find_nas) as spy_nas:
                # 1. First flush checks NAS once; subsequent flushes while dir mtime unchanged are throttled!
                watcher._flush_ready_buffer(drop.resolve())
                watcher._flush_ready_buffer(drop.resolve())
                watcher._flush_ready_buffer(drop.resolve())
                self.assertEqual(spy_nas.call_count, 1)
                self.assertIn("orphan:0100BBB000000000", watcher._logged_hold_keys)

                # 2. Removing the held orphan from drop/ cleans up `_ready_signatures` and `_logged_hold_keys`
                upd_nsp.unlink()
                watcher._flush_ready_buffer(drop.resolve())
                self.assertEqual(watcher._ready_buffer, [])
                self.assertEqual(watcher._ready_signatures, {})
                self.assertEqual(watcher._logged_hold_keys, set())
                self.assertEqual(watcher._orphan_nas_check_state, {})

    def test_list_archive_entries_cache_and_psxpackager_large_stderr(self) -> None:
        from src.archive_handler import _list_archive_entries_cached, list_archive_entries
        from src.runner import run_psxpackager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zpath = root / "cached_test.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("Game.gba", b"GBA" * 64)

            _list_archive_entries_cached.cache_clear()
            with patch(
                "src.archive_handler._list_zip_stdlib",
                wraps=__import__("src.archive_handler", fromlist=["_list_zip_stdlib"])._list_zip_stdlib,
            ) as spy_zip:
                e1 = list_archive_entries(zpath, None)
                e2 = list_archive_entries(zpath, None)
                self.assertEqual(e1, e2)
                self.assertEqual(spy_zip.call_count, 1)
                self.assertEqual(_list_archive_entries_cached.cache_info().hits, 1)

            # On Linux/POSIX, writing >64 KiB to stderr inside `run_psxpackager` must never deadlock
            if sys.platform != "win32":
                work_dir = root / "psx_work"
                work_dir.mkdir()
                noisy_cmd = [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write(('WARN_LINE\\n') * 15000); sys.stderr.flush()",
                ]
                tail = run_psxpackager(noisy_cmd, work_dir=work_dir, status_label="test-stderr")
                self.assertTrue(any("WARN_LINE" in line for line in tail))

    def test_watcher_pairs_untagged_nointro_switch_base_and_wakes_orphan_for_untagged_nas_donor(
        self,
    ) -> None:
        def _make_pfs0_with_tik(tid_hex: str) -> bytes:
            tik_name = f"{tid_hex.lower()}0000000000000000.tik\x00".encode("ascii")
            header = bytearray(16 + 24 + len(tik_name))
            header[0:4] = b"PFS0"
            header[4:8] = (1).to_bytes(4, "little")
            header[8:12] = len(tik_name).to_bytes(4, "little")
            header[40 : 40 + len(tik_name)] = tik_name
            return bytes(header) + (b"\x00" * 64)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_fast_test_config(Path(tmp))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            # Case 1: Untagged No-Intro Base archive + TitleID-bearing Update arrive together in drop/
            base_zip = drop / "Huntdown (Europe) (En,Ja,Fr,De,Es,It,Zh-Hans).zip"
            with zipfile.ZipFile(base_zip, "w") as zf:
                zf.writestr("Huntdown (Europe) (En,Ja,Fr,De,Es,It,Zh-Hans).xci", b"HEAD" * 64)

            upd_nsp = (
                drop
                / "Huntdown (World) (En,Ja,Fr,De,Es,It,Zh-Hans) (v524288) (v1.0.8) (Update).nsp"
            )
            upd_nsp.write_bytes(_make_pfs0_with_tik("0100EBA004726800"))

            watcher1 = PipelineWatcher(cfg)
            watcher1._ready_buffer.extend([base_zip.resolve(), upd_nsp.resolve()])
            watcher1._ready_signatures[base_zip.resolve()] = (1, base_zip.stat().st_size)
            watcher1._ready_signatures[upd_nsp.resolve()] = (1, upd_nsp.stat().st_size)
            watcher1._flush_ready_buffer(drop.resolve())

            self.assertEqual(watcher1.job_queue.qsize(), 1)
            kind, batch = watcher1.job_queue.get_nowait()
            self.assertEqual(kind, "switch")
            self.assertEqual(batch, (base_zip.resolve(), upd_nsp.resolve()))
            self.assertEqual(watcher1._ready_buffer, [])

            # Case 2: Untagged No-Intro Base `.xci` already on NAS -> held Update wakes immediately!
            base_zip.unlink()
            nas_xci = nas_switch / "Huntdown (Europe) (En,Ja,Fr,De,Es,It,Zh-Hans).xci"
            nas_xci.write_bytes(b"HEAD" * 128)

            watcher2 = PipelineWatcher(cfg)
            watcher2._ready_buffer.append(upd_nsp.resolve())
            watcher2._ready_signatures[upd_nsp.resolve()] = (1, upd_nsp.stat().st_size)
            watcher2._flush_ready_buffer(drop.resolve())

            self.assertEqual(watcher2.job_queue.qsize(), 1)
            kind2, batch2 = watcher2.job_queue.get_nowait()
            self.assertEqual(kind2, "single")
            self.assertEqual(batch2, (upd_nsp.resolve(),))
            self.assertEqual(watcher2._ready_buffer, [])


if __name__ == "__main__":
    unittest.main()
