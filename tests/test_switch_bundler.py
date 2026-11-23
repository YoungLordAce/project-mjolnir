"""Unit and integration tests for Switch TitleID family bundling (`src/switch_bundler.py`)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.config_loader import load_config
from src.identifier import identify_directory
from src.models import ConversionArtifact, PostProcessAction
from src.switch_bundler import (
    SwitchContentRole,
    classify_switch_title_id,
    extract_title_ids_from_pfs0_bytes,
    group_candidates_with_switch_families,
    has_explicit_addon_only_tag,
    probe_switch_source,
)
from src.watcher import scan_watch_dir_once

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.example.toml"


def _build_minimal_pfs0_with_tik(rights_id_hex32: str) -> bytes:
    """Build a binary-accurate `PFS0` header containing `<rights_id>.tik` in its string table."""
    tik_name = f"{rights_id_hex32.lower()}.tik\x00".encode("ascii")
    nca_name = b"00112233445566778899aabbccddeeff.nca\x00"
    str_table = tik_name + nca_name
    num_files = 2
    header = bytearray(16 + num_files * 24 + len(str_table))
    header[0:4] = b"PFS0"
    header[4:8] = num_files.to_bytes(4, "little")
    header[8:12] = len(str_table).to_bytes(4, "little")
    # Entry 0 points to str_table offset 0 (`<rights_id>.tik`)
    # Entry 1 points to str_table offset len(tik_name)
    entry1_off = 16 + 24
    header[entry1_off + 16 : entry1_off + 20] = len(tik_name).to_bytes(4, "little")
    header[16 + num_files * 24 :] = str_table
    return bytes(header)


class SwitchBundlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_config = load_config(CONFIG_PATH)

    def _make_isolated_config(self, root: Path):
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
            post_process_action=PostProcessAction.MOVE,
            stability_poll_seconds=0.02,
            stability_checks_required=1,
            min_free_staging_gb=0.01,
        )
        return replace(self.base_config, paths=new_paths, general=new_general)

    def test_horizon_os_title_id_family_math_and_pfs0_tik(self) -> None:
        # Cult of the Lamb exact TitleIDs
        self.assertEqual(
            classify_switch_title_id("01002E7016C46000"),
            ("01002E7016C46000", SwitchContentRole.BASE),
        )
        self.assertEqual(
            classify_switch_title_id("01002E7016C46800"),
            ("01002E7016C46000", SwitchContentRole.UPDATE),
        )
        for dlc_idx in range(1, 6):
            self.assertEqual(
                classify_switch_title_id(f"01002E7016C4700{dlc_idx}"),
                ("01002E7016C46000", SwitchContentRole.DLC),
            )
        # High-index DLC (`+0x1800`) must still classify as DLC, NOT Update!
        self.assertEqual(
            classify_switch_title_id("01002E7016C47800"),
            ("01002E7016C46000", SwitchContentRole.DLC),
        )

        # NSCB tag check
        self.assertTrue(has_explicit_addon_only_tag("Cult of the Lamb [01002E7016C47005]  (5D).xci"))
        self.assertFalse(
            has_explicit_addon_only_tag("Cult of the Lamb [01002E7016C46000] [v1966080] (1G+1U+5D).xci")
        )

        # Binary PFS0 `.tik` extraction when filename lacks `[0100...]`
        pfs0_bytes = _build_minimal_pfs0_with_tik("01002E7016C46800000000000000000D")
        self.assertEqual(extract_title_ids_from_pfs0_bytes(pfs0_bytes), ("01002E7016C46800",))

        # Binary PFS0 ticketless `cnmt.xml` extraction (e.g. Fire Emblem Three Houses)
        xml_content = b'<?xml version="1.0"?><ContentMeta><Type>Application</Type><Id>0x010055d009f78000</Id></ContentMeta>'
        pfs0_with_xml = bytearray(b"PFS0\x01\x00\x00\x00\x00\x00\x00\x00" + b"\x00" * 32 + xml_content)
        pfs0_with_xml[4:8] = (1).to_bytes(4, "little")
        pfs0_with_xml[8:12] = (10).to_bytes(4, "little")
        self.assertEqual(extract_title_ids_from_pfs0_bytes(bytes(pfs0_with_xml)), ("010055D009F78000",))

    def test_multi_game_switch_directory_does_not_falsely_merge_unrelated_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sw_lib = Path(tmp)
            (sw_lib / "Cult of the Lamb [01002E7016C46000].xci").write_bytes(b"HEAD" * 64)
            (sw_lib / "Mario Kart 8 Deluxe [0100152000022000].xci").write_bytes(b"HEAD" * 64)

            items = identify_directory(sw_lib)
            self.assertEqual(len(items), 2)
            self.assertFalse(any(i.is_multifile_switch for i in items))

    def test_cross_archive_grouping_orphan_deferral_and_unified_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_isolated_config(Path(tmp))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            # 1. Drop ONLY the 5-DLC zip + Update .nsp first (Base game missing!)
            dlc_zip = drop / "Cult of the Lamb (5 DLCs).zip"
            with zipfile.ZipFile(dlc_zip, "w") as zf:
                for i in range(1, 6):
                    zf.writestr(f"DLC {i} [01002E7016C4700{i}][v0].nsp", b"PFS0" * 64)
                zf.writestr("Axekin.desktop", b"[Desktop Entry]\nURL=https://example.com\n")

            upd_nsp = drop / "Cult of the Lamb [01002E7016C46800][v1966080](axekin.com).nsp"
            upd_nsp.write_bytes(b"PFS0" * 128)

            info_dlc = probe_switch_source(dlc_zip, cfg)
            self.assertIsNotNone(info_dlc)
            assert info_dlc is not None
            self.assertEqual(info_dlc.primary_family_id, "01002E7016C46000")
            self.assertTrue(info_dlc.is_addon_only)

            # Scan while Base game is absent -> must defer safely without routing `(5D).xci`!
            outcomes_defer, errs_defer = scan_watch_dir_once(cfg, dry_run=False, verify_stability=False)
            self.assertEqual(errs_defer, 0)
            self.assertEqual(len(outcomes_defer), 1)
            self.assertEqual(outcomes_defer[0].routed_files, ())
            self.assertEqual(outcomes_defer[0].quarantined_files, ())
            self.assertTrue(outcomes_defer[0].post_action_taken.startswith("deferred"))
            self.assertTrue(dlc_zip.exists())
            self.assertTrue(upd_nsp.exists())
            self.assertEqual(list(nas_switch.iterdir()), [])

            # 2. Pre-seed NAS with the 3 split files from an older split run to verify auto-replacement
            old_bad_xci = nas_switch / "Cult of the Lamb [01002E7016C47005]  (5D).xci"
            old_upd_nsp = nas_switch / "Cult of the Lamb [01002E7016C46800](axekin.com).nsp"
            old_base_nsp = nas_switch / "Cult of the Lamb [01002E7016C46000](axekin.com).nsp"
            for old_f in (old_bad_xci, old_upd_nsp, old_base_nsp):
                old_f.write_bytes(b"OLD_SPLIT_FILE" * 32)

            # 3. Now Base game archive arrives in `drop/` alongside an unrelated Switch game!
            base_zip = drop / "Cult_of_the_Lamb_[01002E7016C46000][v0](axekin.com).zip"
            with zipfile.ZipFile(base_zip, "w") as zf:
                zf.writestr(
                    "Cult of the Lamb [01002E7016C46000][v0](axekin.com).nsp",
                    b"PFS0_BASE_PAYLOAD" * 128,
                )
                zf.writestr("Axekin.desktop", b"[Desktop Entry]\nURL=https://example.com\n")

            mk8_nsp = drop / "Mario Kart 8 [0100152000022000][v0].nsp"
            mk8_nsp.write_bytes(b"PFS0_MK8" * 64)

            batches = group_candidates_with_switch_families(
                sorted(drop.iterdir(), key=lambda p: p.name.lower()),
                cfg,
            )
            self.assertEqual(len(batches), 2)
            # First batch should be the 3 Cult of the Lamb items ordered `[Base, Update, DLC]`
            self.assertEqual(batches[0], [base_zip.resolve(), upd_nsp.resolve(), dlc_zip.resolve()])
            self.assertEqual(batches[1], [mk8_nsp.resolve()])

            merged_calls = []

            def _fake_convert(item, job_dir: Path, _config):
                merged_calls.append(item)
                if item.is_multifile_switch:
                    out_xci = job_dir / "Cult of the Lamb [01002E7016C46000] [v1966080] (1G+1U+5D).xci"
                    out_xci.write_bytes(b"UNIFIED_XCI_IMAGE" * 256)
                    return ConversionArtifact(
                        system_id="switch",
                        output_files=(out_xci,),
                        converted=True,
                        tool_used="squirrel",
                    )
                return ConversionArtifact(
                    system_id="switch",
                    output_files=(item.primary_file,),
                    converted=False,
                )

            with patch("src.router.convert_identified_item", side_effect=_fake_convert):
                outcomes, errs = scan_watch_dir_once(cfg, dry_run=False, verify_stability=False)

            self.assertEqual(errs, 0)
            self.assertEqual(len(outcomes), 2)
            cotl_out = outcomes[0]

            # Verify Base (`01002E7016C46000`) was passed as primary + 6 companions (1 Update + 5 DLCs)
            self.assertTrue(merged_calls[0].is_multifile_switch)
            self.assertIn("01002E7016C46000", merged_calls[0].primary_file.name)
            self.assertEqual(len(merged_calls[0].companion_files), 6)

            # Verify unified `.xci` is on NAS, zero `.desktop` quarantined, and all 3 split files replaced!
            unified_nas = nas_switch / "Cult of the Lamb [01002E7016C46000] [v1966080] (1G+1U+5D).xci"
            self.assertTrue(unified_nas.is_file())
            self.assertEqual(cotl_out.quarantined_files, ())
            self.assertEqual(len(cotl_out.superseded_files), 3)
            self.assertFalse(old_bad_xci.exists())
            self.assertFalse(old_upd_nsp.exists())
            self.assertFalse(old_base_nsp.exists())

            # Verify all 3 Cult of the Lamb source items moved to `processed/`
            for src_name in (base_zip.name, upd_nsp.name, dlc_zip.name):
                self.assertTrue((cfg.paths.processed_dir / src_name).is_file())
            self.assertEqual(list(cfg.paths.staging_dir.iterdir()), [])

    def test_nas_base_donor_pulled_and_protected_when_drop_dlc_tagged_with_base_tid(self) -> None:
        """
        Verify that when dropped DLC archives are tagged with the Base game's TitleID
        in their filename (e.g. `[3 DLCs][0100606026F12000].zip`), the pipeline:
          1. Recognizes them as add-ons, NOT Base games.
          2. Pulls the existing Base game from the NAS as a donor.
          3. Protects the Base game from ever being deleted by an add-on-only (0G) output.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            cfg = self._make_isolated_config(Path(tmp_root))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            # NAS already has the Base game (v0)
            nas_base = nas_switch / "Final Fantasy Resonance [0100606026F12000][v0].nsp"
            nas_base.write_bytes(b"EXISTING_NAS_BASE_GAME" * 512)

            # Drop has an Update archive
            upd_zip = drop / "Final Fantasy Resonance [0100606026F12800].zip"
            with zipfile.ZipFile(upd_zip, "w") as zf:
                zf.writestr(
                    "Final Fantasy Resonance [0100606026F12800][v65536].nsp",
                    _build_minimal_pfs0_with_tik("0100606026F128000000000000000000"),
                )

            # Drop has a DLC archive tagged with Base TID in brackets and `[3 DLCs]` in name
            dlc_zip = drop / "Final Fantasy Resonance [3 DLCs][0100606026F12000].zip"
            with zipfile.ZipFile(dlc_zip, "w") as zf:
                zf.writestr(
                    "Final Fantasy Resonance [DLC - Pack 1][0100606026F12000].nsp",
                    _build_minimal_pfs0_with_tik("0100606026F130010000000000000000"),
                )
                zf.writestr(
                    "Final Fantasy Resonance [DLC - Pack 2][0100606026F12000].nsp",
                    _build_minimal_pfs0_with_tik("0100606026F130020000000000000000"),
                )

            merged_calls = []

            def _fake_convert(item, job_dir: Path, _config):
                merged_calls.append(item)
                # Output unified (1G+1U+2D).xci
                out_xci = job_dir / "Final Fantasy Resonance [0100606026F12000] [v65536] (1G+1U+2D).xci"
                out_xci.write_bytes(b"UNIFIED_FINAL_FANTASY_XCI" * 512)
                return ConversionArtifact(
                    system_id="switch",
                    output_files=(out_xci,),
                    converted=True,
                    tool_used="squirrel",
                )

            with patch("src.router.convert_identified_item", side_effect=_fake_convert):
                outcomes, errs = scan_watch_dir_once(cfg, dry_run=False, verify_stability=False)

            self.assertEqual(errs, 0)
            self.assertEqual(len(outcomes), 1)

            # Verify the NAS Base donor was pulled into staging and included in merge
            self.assertEqual(len(merged_calls), 1)
            id_item = merged_calls[0]
            all_files = [id_item.primary_file, *id_item.companion_files]
            self.assertTrue(any("nas_donor" in str(p) or "[v0]" in p.name for p in all_files))

            # Verify the unified XCI was routed and the old standalone base NSP replaced
            unified_nas = nas_switch / "Final Fantasy Resonance [0100606026F12000] [v65536] (1G+1U+2D).xci"
            self.assertTrue(unified_nas.is_file())
            self.assertFalse(nas_base.exists())

    def test_addon_only_output_never_deletes_nas_base_game(self) -> None:
        """
        Verify that if an add-on only container (0G, e.g. `(1U+2D)`) is produced,
        the safety guard in router.py prevents deleting the existing Base game on the NAS.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            cfg = self._make_isolated_config(Path(tmp_root))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            nas_base = nas_switch / "Game [0100606026F12000][v0].nsp"
            nas_base.write_bytes(b"EXISTING_NAS_BASE_GAME" * 512)

            upd_zip = drop / "Game [0100606026F12800].zip"
            with zipfile.ZipFile(upd_zip, "w") as zf:
                zf.writestr(
                    "Game [0100606026F12800][v65536].nsp",
                    _build_minimal_pfs0_with_tik("0100606026F128000000000000000000"),
                )

            # Fake conversion returns an add-on-only file (1U) without base game
            def _fake_convert_addon_only(item, job_dir: Path, _config):
                out_xci = job_dir / "Game [0100606026F12800] (1U).xci"
                out_xci.write_bytes(b"ADDON_ONLY_IMAGE" * 256)
                return ConversionArtifact(
                    system_id="switch",
                    output_files=(out_xci,),
                    converted=True,
                    tool_used="squirrel",
                )

            with patch("src.router.convert_identified_item", side_effect=_fake_convert_addon_only):
                outcomes, errs = scan_watch_dir_once(cfg, dry_run=False, verify_stability=False)

            self.assertEqual(errs, 0)
            self.assertEqual(len(outcomes), 1)

            # The add-on only file was routed, BUT the Base game on the NAS was PROTECTED and NOT deleted!
            self.assertTrue((nas_switch / "Game [0100606026F12800] (1U).xci").is_file())
            self.assertTrue(nas_base.is_file(), "Base game on NAS must NEVER be deleted by an addon-only output!")

    def test_bounded_lru_caches_on_probe_switch_source_and_find_nas_switch_family_files(self) -> None:
        from src.switch_bundler import (
            _extract_switch_tids_cached,
            _find_nas_switch_family_files_cached,
            _probe_switch_file_cached,
            find_nas_switch_family_files,
        )

        self.assertEqual(_extract_switch_tids_cached.cache_info().maxsize, 4096)
        self.assertEqual(_probe_switch_file_cached.cache_info().maxsize, 4096)
        self.assertEqual(_find_nas_switch_family_files_cached.cache_info().maxsize, 64)

        with tempfile.TemporaryDirectory() as tmp_root:
            cfg = self._make_isolated_config(Path(tmp_root))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            upd_zip = drop / "CacheTest [0100AAA000000800].zip"
            with zipfile.ZipFile(upd_zip, "w") as zf:
                zf.writestr(
                    "CacheTest [0100AAA000000800][v65536].nsp",
                    _build_minimal_pfs0_with_tik("0100AAA0000008000000000000000000"),
                )

            _probe_switch_file_cached.cache_clear()
            _find_nas_switch_family_files_cached.cache_clear()

            with patch("src.switch_bundler.list_archive_entries", wraps=__import__("src.archive_handler", fromlist=["list_archive_entries"]).list_archive_entries) as spy_list:
                info1 = probe_switch_source(upd_zip, cfg)
                info2 = probe_switch_source(upd_zip, cfg)
                self.assertEqual(info1, info2)
                self.assertEqual(spy_list.call_count, 1)
                self.assertEqual(_probe_switch_file_cached.cache_info().hits, 1)

            # Repeated NAS family polls on unchanged directory must hit `_find_nas_switch_family_files_cached`
            donor1, matches1 = find_nas_switch_family_files(nas_switch, "0100AAA000000000", cfg)
            donor2, matches2 = find_nas_switch_family_files(nas_switch, "0100AAA000000000", cfg)
            self.assertIsNone(donor1)
            self.assertEqual(matches1, [])
            self.assertEqual(donor2, donor1)
            self.assertEqual(matches2, matches1)
            self.assertGreaterEqual(_find_nas_switch_family_files_cached.cache_info().hits, 1)

            # Adding a Base donor to `nas_switch` must automatically invalidate the directory signature and find it
            nas_base = nas_switch / "CacheTest [0100AAA000000000][v0].nsp"
            nas_base.write_bytes(_build_minimal_pfs0_with_tik("0100AAA0000000000000000000000000"))
            donor3, matches3 = find_nas_switch_family_files(nas_switch, "0100AAA000000000", cfg)
            self.assertEqual(donor3, nas_base)
            self.assertEqual(matches3, [nas_base])

    def test_hybrid_folder_with_untagged_nointro_xci_archive_and_loose_update_dlc(self) -> None:
        """
        Verify a Switch release folder mixing an untagged No-Intro Base `.xci` archive,
        a loose scene Update `.nsp` in a subfolder, a loose `(DLC).nsp`, and `.json`/`.jpg`/`.html`
        sidecars recognizes `has_base == True` and merges all 3 with the Base `.xci` first.
        """
        with tempfile.TemporaryDirectory() as tmp_root:
            cfg = self._make_isolated_config(Path(tmp_root))
            drop = cfg.paths.watch_dir
            nas_switch = cfg.paths.library_root / "switch"
            nas_switch.mkdir(parents=True, exist_ok=True)

            rel_dir = drop / "Sea of Stars (USA, Europe) (Rev 1)"
            upd_sub = rel_dir / "Sea_of_Stars_Update_v2.0.59001_NSW-VENOM"
            covers_sub = rel_dir / "imgs" / "covers"
            upd_sub.mkdir(parents=True, exist_ok=True)
            covers_sub.mkdir(parents=True, exist_ok=True)

            # 1. Untagged No-Intro Base `.xci` inside archive
            base_arc = rel_dir / "Sea of Stars (USA, Europe) (Rev 1).zip"
            with zipfile.ZipFile(base_arc, "w") as zf:
                zf.writestr("Sea of Stars (USA, Europe) (Rev 1).xci", b"XCI_BASE_CARTRIDGE_DUMP" * 256)

            # 2. Loose DLC `.nsp` (`01008C0016545001`)
            dlc_nsp = rel_dir / "Sea of Stars - Throes of the Watchmaker (World) (v524288) (DLC).nsp"
            dlc_nsp.write_bytes(_build_minimal_pfs0_with_tik("01008C00165450010000000000000000"))

            # 3. Loose Update `.nsp` (`01008C0016544800`) inside scene subfolder
            upd_nsp = upd_sub / "v-sea_of_stars_v786432.nsp"
            upd_nsp.write_bytes(_build_minimal_pfs0_with_tik("01008C00165448000000000000000000"))

            # 4. Sidecars that must be ignored
            (rel_dir / "BLUEROMS.WS.html").write_text("<html></html>", encoding="utf-8")
            (rel_dir / "metadata.json").write_text('{"platform": "switch"}', encoding="utf-8")
            (covers_sub / "cover1.jpg").write_bytes(b"\xff\xd8\xff\xe0JFIF")
            (upd_sub / "v-sea_of_stars_v786432.nfo").write_text("NFO", encoding="utf-8")

            info = probe_switch_source(rel_dir, cfg)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.primary_family_id, "01008C0016544000")
            self.assertTrue(info.has_base)
            self.assertFalse(info.is_addon_only)
            self.assertEqual(info.package_count, 3)

            merged_items = []

            def _fake_convert(item, job_dir: Path, _config):
                merged_items.append(item)
                out_xci = job_dir / "Sea of Stars [01008C0016544000] [v786432] (1G+1U+1D).xci"
                out_xci.write_bytes(b"MERGED_SEA_OF_STARS_XCI" * 256)
                return ConversionArtifact(
                    system_id="switch",
                    output_files=(out_xci,),
                    converted=True,
                    tool_used="squirrel",
                )

            with patch("src.router.convert_identified_item", side_effect=_fake_convert):
                outcomes, errs = scan_watch_dir_once(cfg, dry_run=False, verify_stability=False)

            self.assertEqual(errs, 0)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(merged_items), 1)

            # Base `.xci` must be ordered first (`primary_file`), followed by Update `.nsp`, then DLC `.nsp`
            id_item = merged_items[0]
            self.assertEqual(id_item.primary_file.name, "Sea of Stars (USA, Europe) (Rev 1).xci")
            self.assertEqual(len(id_item.companion_files), 2)
            self.assertEqual(id_item.companion_files[0].name, "v-sea_of_stars_v786432.nsp")
            self.assertEqual(
                id_item.companion_files[1].name,
                "Sea of Stars - Throes of the Watchmaker (World) (v524288) (DLC).nsp",
            )


if __name__ == "__main__":
    unittest.main()
