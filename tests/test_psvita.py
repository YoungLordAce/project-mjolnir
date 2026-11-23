"""Unit and integration tests for PlayStation Vita (Vita3K) identification, conversion, and routing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile

from src.archive_handler import (
    extract_header_preview_to_staging,
    inspect_archive,
)
from src.config_loader import load_config
from src.converter import describe_planned_action
from src.identifier import (
    decode_zrif_to_work_bin,
    encode_work_bin_to_zrif,
    identify_directory,
    identify_single_file,
    lookup_vita_tsv_zrif,
    parse_sfo_bytes,
)
from src.models import PostProcessAction
from src.router import ingest_target
from src.watcher import discover_watch_candidates, find_claiming_descriptor_in_dir


def build_synthetic_sfo(fields: dict[str, str | int]) -> bytes:
    """Construct a valid Sony `\\x00PSF` (v1.1) binary `param.sfo` buffer from `fields`."""
    keys = sorted(fields.keys())
    key_blobs: list[bytes] = []
    data_blobs: list[bytes] = []
    index_entries: list[bytes] = []

    key_offset = 0
    data_offset = 0

    for k in keys:
        k_bytes = k.encode("utf-8") + b"\x00"
        val = fields[k]
        if isinstance(val, int):
            param_fmt = 0x0404  # uint32
            raw_data = struct.pack("<I", val)
            param_len = 4
            param_max_len = 4
        else:
            param_fmt = 0x0204  # UTF-8 null-terminated string
            encoded = str(val).encode("utf-8") + b"\x00"
            param_len = len(encoded)
            # Align max_len to 4-byte boundary
            pad = (4 - (param_len % 4)) % 4
            raw_data = encoded + (b"\x00" * pad)
            param_max_len = len(raw_data)

        index_entries.append(
            struct.pack(
                "<HHIII",
                key_offset,
                param_fmt,
                param_len,
                param_max_len,
                data_offset,
            )
        )
        key_blobs.append(k_bytes)
        data_blobs.append(raw_data)
        key_offset += len(k_bytes)
        data_offset += len(raw_data)

    key_table = b"".join(key_blobs)
    # Pad key_table to 4-byte alignment
    if len(key_table) % 4 != 0:
        key_table += b"\x00" * (4 - (len(key_table) % 4))

    header_len = 20 + (16 * len(keys))
    key_table_start = header_len
    data_table_start = key_table_start + len(key_table)

    header = struct.pack(
        "<4sIIII",
        b"\x00PSF",
        0x00000101,
        key_table_start,
        data_table_start,
        len(keys),
    )
    return header + b"".join(index_entries) + key_table + b"".join(data_blobs)


def build_synthetic_vita_pkg(content_id: str = "UP0000-PCSE00120_00-PERSONA4GOLDEN00") -> bytes:
    """Construct a synthetic Sony `.pkg` header with `\\x7fPKG` magic and Content ID at `0x30`."""
    buf = bytearray(256)
    buf[0:4] = b"\x7fPKG"
    # Big-endian pkg_revision (0x8000) and pkg_type (0x0002 = PSP/Vita)
    buf[4:8] = b"\x80\x00\x00\x02"
    cid_bytes = content_id.encode("ascii")[:36]
    buf[0x30 : 0x30 + len(cid_bytes)] = cid_bytes
    return bytes(buf)


class PsVitaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        base_cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
        self.drop_dir = self.root / "drop"
        self.staging_dir = self.root / "staging"
        self.lib_dir = self.root / "library"
        self.proc_dir = self.root / "processed"
        self.quar_dir = self.root / "quarantine"
        for d in (self.drop_dir, self.staging_dir, self.lib_dir, self.proc_dir, self.quar_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.config = replace(
            base_cfg,
            paths=replace(
                base_cfg.paths,
                watch_dir=self.drop_dir,
                staging_dir=self.staging_dir,
                library_root=self.lib_dir,
                processed_dir=self.proc_dir,
                quarantine_dir=self.quar_dir,
            ),
            general=replace(
                base_cfg.general,
                post_process_action=PostProcessAction.MOVE,
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_sfo_bytes_and_vpk_inspection(self) -> None:
        """Verify binary `\\x00PSF` (`param.sfo`) parsing and `.vpk` package inspection/routing."""
        sfo_bytes = build_synthetic_sfo({
            "TITLE_ID": "PCSE00120",
            "APP_VER": "01.00",
            "CATEGORY": "gd",
            "TITLE": "Persona 4: Golden",
            "ATTRIBUTE": 32,
        })
        parsed = parse_sfo_bytes(sfo_bytes)
        self.assertEqual(parsed["TITLE_ID"], "PCSE00120")
        self.assertEqual(parsed["APP_VER"], "01.00")
        self.assertEqual(parsed["CATEGORY"], "gd")
        self.assertEqual(parsed["TITLE"], "Persona 4: Golden")
        self.assertEqual(parsed["ATTRIBUTE"], 32)

        vpk_path = self.drop_dir / "Persona 4 Golden.vpk"
        with zipfile.ZipFile(vpk_path, "w") as zf:
            zf.writestr("sce_sys/param.sfo", sfo_bytes)
            zf.writestr("eboot.bin", b"SCE_VITA_EBOOT_PAYLOAD" * 64)

        res = identify_single_file(vpk_path)
        self.assertEqual(res.system_id, "psvita")
        self.assertEqual(res.vita_title_id, "PCSE00120")
        self.assertEqual(res.vita_app_version, "01.00")
        self.assertEqual(res.vita_clean_title, "Persona 4 - Golden")

        outcome = ingest_target(vpk_path, self.config)
        self.assertEqual(len(outcome.routed_files), 2)
        self.assertEqual(outcome.routed_files[0].name, "Persona 4 Golden.vpk")
        self.assertTrue(outcome.routed_files[0].is_file())
        self.assertEqual(outcome.routed_files[0].parent, self.lib_dir / "psvita")
        self.assertEqual(outcome.routed_files[1].name, "Persona 4 - Golden.dpt")
        self.assertEqual(outcome.routed_files[1].read_text(encoding="utf-8"), "[vita_game_id] PCSE00120\n")

    def test_nonpdrm_archive_preview_placeholders_and_vita3k_zip_repack(self) -> None:
        """Verify 0-byte preview placeholders during inspect and clean Vita3K `.zip` repacking."""
        sfo_bytes = build_synthetic_sfo({
            "TITLE_ID": "PCSA00011",
            "APP_VER": "01.00",
            "CATEGORY": "gd",
            "TITLE": "Uncharted: Golden Abyss",
        })
        arc_path = self.drop_dir / "Uncharted_Golden_Abyss_NoNpDrm.zip"
        with zipfile.ZipFile(arc_path, "w") as zf:
            zf.writestr("PCSA00011/sce_sys/param.sfo", sfo_bytes)
            zf.writestr("PCSA00011/eboot.bin", b"VITA_EBOOT_DATA" * 1024)
            zf.writestr("PCSA00011/data/archive.psarc", b"LARGE_ASSET_PAYLOAD" * 2048)
            zf.writestr("PCSA00011/ROMsFun.com.url", b"[InternetShortcut]\nURL=https://example.com\n")
            zf.writestr("Downloaded from ROMsFun.txt", b"Watermark text")

        arc_info = inspect_archive(arc_path, self.config)
        self.assertFalse(arc_info.is_arcade)

        # 1. Verify header preview only streams `param.sfo` and touches other files as 0-byte placeholders
        preview_dir, _ = extract_header_preview_to_staging(
            arc_info,
            self.config,
            target_dir=self.staging_dir / "test_vita_prev",
        )
        self.assertGreater((preview_dir / "PCSA00011" / "sce_sys" / "param.sfo").stat().st_size, 0)
        self.assertEqual((preview_dir / "PCSA00011" / "eboot.bin").stat().st_size, 0)
        self.assertEqual((preview_dir / "PCSA00011" / "data" / "archive.psarc").stat().st_size, 0)

        id_items = identify_directory(preview_dir)
        self.assertEqual(len(id_items), 1)
        self.assertEqual(id_items[0].system_id, "psvita")
        self.assertEqual(id_items[0].vita_title_id, "PCSA00011")
        self.assertEqual(id_items[0].vita_clean_title, "Uncharted - Golden Abyss")

        planned = describe_planned_action(id_items[0], self.config, from_archive=True, archive_ext=".zip")
        self.assertIn(
            "Repack Vita3K .zip (Uncharted - Golden Abyss [PCSA00011] [v01.00].zip + Uncharted - Golden Abyss.dpt)",
            planned,
        )

        # 2. Live ingestion: verify repacked `.zip` structure, Cocoon `.dpt` tag, and stripped watermark sidecars
        outcome = ingest_target(arc_path, self.config)
        self.assertEqual(len(outcome.quarantined_files), 0)
        self.assertEqual(len(outcome.routed_files), 2)
        out_zip = outcome.routed_files[0]
        out_dpt = outcome.routed_files[1]
        self.assertEqual(out_zip.name, "Uncharted - Golden Abyss [PCSA00011] [v01.00].zip")
        self.assertTrue(out_zip.is_file())
        self.assertEqual(out_dpt.name, "Uncharted - Golden Abyss.dpt")
        self.assertEqual(out_dpt.read_text(encoding="utf-8"), "[vita_game_id] PCSA00011\n")

        with zipfile.ZipFile(out_zip, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("PCSA00011/sce_sys/param.sfo", names)
            self.assertIn("PCSA00011/eboot.bin", names)
            self.assertIn("PCSA00011/data/archive.psarc", names)
            # Watermark sidecars inside and outside the package root must be stripped
            self.assertFalse(any("romsfun" in n.lower() for n in names))
            self.assertGreater(len(zf.read("PCSA00011/eboot.bin")), 1000)

    def test_multi_package_base_update_dlc_vita_bundle(self) -> None:
        """Verify an archive containing Base (`gd`) + Patch (`gp`) + DLC (`ac`) bundles into one `.zip`."""
        base_sfo = build_synthetic_sfo({
            "TITLE_ID": "PCSE00986",
            "APP_VER": "01.00",
            "CATEGORY": "gd",
            "TITLE": "Salt and Sanctuary",
        })
        patch_sfo = build_synthetic_sfo({
            "TITLE_ID": "PCSE00986",
            "APP_VER": "01.03",
            "CATEGORY": "gp",
            "TITLE": "Salt and Sanctuary",
        })
        dlc_sfo = build_synthetic_sfo({
            "TITLE_ID": "PCSE00986",
            "VERSION": "01.00",
            "CATEGORY": "ac",
            "TITLE": "Salt and Sanctuary Extra Pack",
        })

        bundle_arc = self.drop_dir / "Salt_and_Sanctuary_Complete.zip"
        with zipfile.ZipFile(bundle_arc, "w") as zf:
            zf.writestr("app/PCSE00986/sce_sys/param.sfo", base_sfo)
            zf.writestr("app/PCSE00986/eboot.bin", b"BASE_EBOOT_BYTES" * 32)
            zf.writestr("patch/PCSE00986/sce_sys/param.sfo", patch_sfo)
            zf.writestr("patch/PCSE00986/eboot.bin", b"PATCH_EBOOT_BYTES" * 32)
            zf.writestr("addcont/PCSE00986/DLC001/sce_sys/param.sfo", dlc_sfo)
            zf.writestr("addcont/PCSE00986/DLC001/dlc.dat", b"DLC_BYTES" * 16)

        outcome = ingest_target(bundle_arc, self.config)
        self.assertEqual(len(outcome.routed_files), 2)
        out_zip = outcome.routed_files[0]
        out_dpt = outcome.routed_files[1]
        # Highest version from Patch (`01.03`) should be reflected in output filename
        self.assertEqual(out_zip.name, "Salt and Sanctuary [PCSE00986] [v01.03].zip")
        self.assertEqual(out_dpt.name, "Salt and Sanctuary.dpt")
        self.assertEqual(out_dpt.read_text(encoding="utf-8"), "[vita_game_id] PCSE00986\n")

        with zipfile.ZipFile(out_zip, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("app/PCSE00986/sce_sys/param.sfo", names)
            self.assertIn("patch/PCSE00986/sce_sys/param.sfo", names)
            self.assertIn("addcont/PCSE00986/DLC001/sce_sys/param.sfo", names)

    def test_sony_pkg_with_work_bin_companion_and_watcher_claiming(self) -> None:
        """Verify Sony `.pkg` + `work.bin` sniffing, watcher companion claiming, and `pkg2zip` execution."""
        pkg_path = self.drop_dir / "Muramasa Rebirth [PCSE00240].pkg"
        work_bin = self.drop_dir / "work.bin"
        pkg_path.write_bytes(build_synthetic_vita_pkg("UP0000-PCSE00240_00-MURAMASAREBIRTH0"))
        work_bin.write_bytes(b"VITA_WORK_BIN_LICENSE_PAYLOAD" * 8)

        # 1. Watcher must claim `work.bin` under `pkg_path` rather than queuing `work.bin` as a raw `.bin`
        owner = find_claiming_descriptor_in_dir(work_bin, self.drop_dir)
        self.assertEqual(owner, pkg_path.resolve())
        candidates = discover_watch_candidates(self.drop_dir, self.config)
        self.assertEqual(candidates, [pkg_path.resolve()])

        # 2. Identify `.pkg` and verify `work.bin` is attached as companion
        res = identify_single_file(pkg_path)
        self.assertEqual(res.system_id, "psvita")
        self.assertEqual(res.vita_title_id, "PCSE00240")
        self.assertEqual(res.vita_clean_title, "Muramasa Rebirth")
        self.assertEqual(res.companion_files, (work_bin.resolve(),))

        # 3. Configure a mock `pkg2zip` script that produces an unpacked `.zip` and verify `work.bin` injection
        mock_pkg2zip = self.root / "mock_pkg2zip.cmd" if sys.platform == "win32" else self.root / "mock_pkg2zip.sh"
        helper_py = self.root / "make_pkg_zip.py"
        helper_py.write_text(
            "import zipfile\n"
            "with zipfile.ZipFile('out.zip', 'w') as zf:\n"
            "    zf.writestr('app/PCSE00240/sce_sys/param.sfo', b'\\x00PSF')\n"
            "    zf.writestr('app/PCSE00240/eboot.bin', b'EBOOT')\n",
            encoding="utf-8",
        )
        if sys.platform == "win32":
            mock_pkg2zip.write_text(f'@"{sys.executable}" "{helper_py}"\n', encoding="utf-8")
        else:
            mock_pkg2zip.write_text(f'#!/bin/sh\n"{sys.executable}" "{helper_py}"\n', encoding="utf-8")
            mock_pkg2zip.chmod(0o755)

        cfg_with_pkg2zip = replace(
            self.config,
            binaries=replace(self.config.binaries, pkg2zip=mock_pkg2zip),
        )
        outcome = ingest_target(pkg_path, cfg_with_pkg2zip)
        self.assertEqual(len(outcome.routed_files), 2)
        out_zip = outcome.routed_files[0]
        out_dpt = outcome.routed_files[1]
        self.assertEqual(out_zip.name, "Muramasa Rebirth [PCSE00240].zip")
        self.assertEqual(out_dpt.name, "Muramasa Rebirth.dpt")
        self.assertEqual(out_dpt.read_text(encoding="utf-8"), "[vita_game_id] PCSE00240\n")

        with zipfile.ZipFile(out_zip, "r") as zf:
            self.assertIn("app/PCSE00240/sce_sys/package/work.bin", zf.namelist())

        # Both `.pkg` and companion `work.bin` should have been moved to `processed/`
        self.assertFalse(pkg_path.exists())
        self.assertFalse(work_bin.exists())
        self.assertTrue((self.proc_dir / pkg_path.name).is_file())
        self.assertTrue((self.proc_dir / "work.bin").is_file())

    def test_standalone_update_and_dlc_route_to_updates_dlc_subdir(self) -> None:
        """Verify standalone PS Vita Updates (`gp`) and DLC (`ac`) route to `psvita/_updates_dlc/`."""
        upd_sfo = build_synthetic_sfo({
            "TITLE_ID": "PCSB00408",
            "APP_VER": "01.09",
            "CATEGORY": "gp",
            "TITLE": "Dragon's Crown",
        })
        # Embed SFO at offset 0x200 inside a synthetic Sony `.pkg`
        pkg_header = bytearray(build_synthetic_vita_pkg("EP1063-PCSB00408_00-DRAGONSCROWN0001"))
        pkg_header.extend(b"\x00" * 256)
        pkg_header.extend(upd_sfo)

        upd_pkg = self.drop_dir / "Dragon s Crown [UPDATE 01.09][PCSB00408](axekin.com).pkg"
        upd_pkg.write_bytes(bytes(pkg_header))

        cfg_no_pkg2zip = replace(
            self.config,
            binaries=replace(self.config.binaries, pkg2zip=None),
        )
        outcome = ingest_target(upd_pkg, cfg_no_pkg2zip)
        self.assertEqual(len(outcome.routed_files), 1)
        routed_upd = outcome.routed_files[0]
        self.assertEqual(routed_upd.name, "Dragon's Crown [PCSB00408] [Update v01.09].pkg")
        self.assertEqual(routed_upd.parent, self.lib_dir / "psvita" / "_updates_dlc")

    def test_zrif_roundtrip_and_psv_games_tsv_auto_work_bin_generation(self) -> None:
        """Verify `zRIF` <-> `work.bin` decompression and automatic `PSV_GAMES.tsv` license generation."""
        sample_rif = bytearray(512)
        sample_rif[0x10:0x34] = b"EP1063-PCSB00408_00-DRAGONSCROWN0001"
        zrif_str = encode_work_bin_to_zrif(bytes(sample_rif))
        decoded_rif = decode_zrif_to_work_bin(zrif_str)
        self.assertEqual(len(decoded_rif), 512)
        self.assertEqual(decoded_rif, bytes(sample_rif))

        # Write a synthetic `PSV_GAMES.tsv` in `drop_dir` and verify automatic lookup & `_work.bin` routing
        tsv_path = self.drop_dir / "PSV_GAMES.tsv"
        tsv_path.write_text(
            "Title ID\tRegion\tName\tPKG direct link\tzRIF\tContent ID\n"
            f"PCSB00408\tEU\tDragon's Crown\thttp://example.com/dc.pkg\t{zrif_str}\tEP1063-PCSB00408_00-DRAGONSCROWN0001\n",
            encoding="utf-8",
        )
        found_zrif, found_title, _ = lookup_vita_tsv_zrif(
            "EP1063-PCSB00408_00-DRAGONSCROWN0001",
            "PCSB00408",
            extra_dirs=(self.drop_dir,),
        )
        self.assertEqual(found_zrif, zrif_str)
        self.assertEqual(found_title, "Dragon's Crown")

        base_pkg = self.drop_dir / "Dragon's Crown [PCSB00408].pkg"
        base_pkg.write_bytes(build_synthetic_vita_pkg("EP1063-PCSB00408_00-DRAGONSCROWN0001"))

        cfg_no_pkg2zip = replace(
            self.config,
            binaries=replace(self.config.binaries, pkg2zip=None),
        )
        outcome = ingest_target(base_pkg, cfg_no_pkg2zip)
        routed_names = {p.name: p.parent for p in outcome.routed_files}
        # Base `.pkg` and `.dpt` route to `psvita/`, while generated `_work.bin` and `.zrif.txt` route to `psvita/_updates_dlc/`
        self.assertEqual(routed_names.get("Dragon's Crown [PCSB00408].pkg"), self.lib_dir / "psvita")
        self.assertEqual(routed_names.get("Dragon's Crown.dpt"), self.lib_dir / "psvita")
        self.assertEqual(
            (self.lib_dir / "psvita" / "Dragon's Crown.dpt").read_text(encoding="utf-8"),
            "[vita_game_id] PCSB00408\n",
        )
        self.assertEqual(
            routed_names.get("Dragon's Crown [PCSB00408]_work.bin"),
            self.lib_dir / "psvita" / "_updates_dlc",
        )
        self.assertEqual(
            routed_names.get("Dragon's Crown [PCSB00408].zrif.txt"),
            self.lib_dir / "psvita" / "_updates_dlc",
        )

    def test_flat_vita_archive_not_misclassified_as_arcade(self) -> None:
        """Verify flat PS Vita archives (depth <= 1 with `.bin` assets + `sce_sys/param.sfo`) are not hijacked as `fbneo`."""
        sfo_bytes = build_synthetic_sfo({
            "TITLE_ID": "PCSE00023",
            "APP_VER": "01.00",
            "CATEGORY": "gd",
            "TITLE": "Gravity Rush",
        })
        flat_zip = self.drop_dir / "GravityRush_Flat.zip"
        with zipfile.ZipFile(flat_zip, "w") as zf:
            zf.writestr("sce_sys/param.sfo", sfo_bytes)
            zf.writestr("eboot.bin", b"VITA_EBOOT" * 128)
            zf.writestr("data0.bin", b"VITA_DATA0" * 128)
            zf.writestr("data1.bin", b"VITA_DATA1" * 128)
            zf.writestr(".DS_Store", b"MACOS_JUNK")

        arc_info = inspect_archive(flat_zip, self.config)
        self.assertFalse(arc_info.is_arcade)
        self.assertIsNone(arc_info.arcade_system_id)

        outcome = ingest_target(flat_zip, self.config)
        self.assertEqual(outcome.quarantined_files, ())
        routed_names = {p.name: p.parent for p in outcome.routed_files}
        self.assertEqual(
            routed_names.get("Gravity Rush [PCSE00023] [v01.00].zip"),
            self.lib_dir / "psvita",
        )
        self.assertEqual(
            routed_names.get("Gravity Rush.dpt"),
            self.lib_dir / "psvita",
        )
        with zipfile.ZipFile(self.lib_dir / "psvita" / "Gravity Rush [PCSE00023] [v01.00].zip", "r") as zf:
            self.assertFalse(any(".ds_store" in n.lower() for n in zf.namelist()))

    def test_pkg_zrif_txt_companion_routing_and_ds_store_filtering(self) -> None:
        """Verify `zrif.txt` routes into `_updates_dlc/` without duplicating and `.DS_Store` is never quarantined."""
        sample_rif = bytearray(512)
        sample_rif[0x10:0x34] = b"UP0000-PCSE00240_00-MURAMASAREBIRTH0"
        zrif_str = encode_work_bin_to_zrif(bytes(sample_rif))

        pkg_path = self.drop_dir / "Muramasa Rebirth [PCSE00240].pkg"
        pkg_path.write_bytes(build_synthetic_vita_pkg("UP0000-PCSE00240_00-MURAMASAREBIRTH0"))
        zrif_txt = self.drop_dir / "zrif.txt"
        zrif_txt.write_text(f"{zrif_str}\n", encoding="utf-8")

        cfg_no_pkg2zip = replace(
            self.config,
            binaries=replace(self.config.binaries, pkg2zip=None),
            paths=replace(self.config.paths, project_root=self.root),
        )
        outcome = ingest_target(pkg_path, cfg_no_pkg2zip)
        routed_names = {p.name: p.parent for p in outcome.routed_files}
        self.assertEqual(routed_names.get("Muramasa Rebirth [PCSE00240].pkg"), self.lib_dir / "psvita")
        self.assertEqual(routed_names.get("Muramasa Rebirth.dpt"), self.lib_dir / "psvita")
        # `zrif.txt` must route to `_updates_dlc/` and NOT leak to `psvita/` root or generate a duplicate `.zrif.txt`
        self.assertEqual(routed_names.get("zrif.txt"), self.lib_dir / "psvita" / "_updates_dlc")
        self.assertNotIn("Muramasa Rebirth [PCSE00240].zrif.txt", routed_names)
        self.assertEqual(
            routed_names.get("Muramasa Rebirth [PCSE00240]_work.bin"),
            self.lib_dir / "psvita" / "_updates_dlc",
        )

        # Standalone folder containing `.DS_Store` alongside a ROM must ignore `.DS_Store` (no quarantine)
        mac_folder = self.drop_dir / "MacDrop"
        mac_folder.mkdir()
        (mac_folder / "Metroid.gba").write_bytes(b"GBA_PAYLOAD" * 64)
        (mac_folder / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        mac_out = ingest_target(mac_folder, cfg_no_pkg2zip)
        self.assertEqual(mac_out.quarantined_files, ())
        self.assertEqual(mac_out.routed_files, (self.lib_dir / "gba" / "Metroid.gba",))

    def test_lookup_vita_tsv_zrif_anchors_on_project_root(self) -> None:
        """Verify `lookup_vita_tsv_zrif` resolves `<project_root>/tools/psvita/*.tsv` instead of module path."""
        sample_rif = bytearray(512)
        sample_rif[0x10:0x34] = b"UP9999-PCSA99999_00-CUSTOMROOTTEST00"
        zrif_str = encode_work_bin_to_zrif(bytes(sample_rif))

        # Empty custom project_root must NOT leak entries from the repository's own `tools/psvita/`
        empty_root = self.root / "empty_proj"
        empty_root.mkdir()
        z_none, _, _ = lookup_vita_tsv_zrif(None, "PCSA00011", project_root=empty_root)
        self.assertIsNone(z_none)

        # Custom project_root with its own TSV must be discovered
        custom_root = self.root / "custom_proj"
        custom_tsv_dir = custom_root / "tools" / "psvita"
        custom_tsv_dir.mkdir(parents=True)
        (custom_tsv_dir / "PSV_GAMES.tsv").write_text(
            "Title ID\tRegion\tName\tPKG direct link\tzRIF\tContent ID\n"
            f"PCSA99999\tUS\tCustom Root Game\thttp://example.com/g.pkg\t{zrif_str}\tUP9999-PCSA99999_00-CUSTOMROOTTEST00\n",
            encoding="utf-8",
        )
        found_zrif, found_title, tsv_name = lookup_vita_tsv_zrif(
            "UP9999-PCSA99999_00-CUSTOMROOTTEST00",
            "PCSA99999",
            project_root=custom_root,
        )
        self.assertEqual(found_zrif, zrif_str)
        self.assertEqual(found_title, "Custom Root Game")
        self.assertEqual(tsv_name, "PSV_GAMES.tsv")


if __name__ == "__main__":
    unittest.main()
