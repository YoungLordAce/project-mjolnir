"""Automated unit & integration tests for Phase 2 (`arcade_db`, `archive_handler`, `identifier`)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from src.archive_handler import extract_archive_to_staging, inspect_archive
from src.config_loader import load_config
from src.identifier import identify_directory, identify_single_file
from src.models import DetectionMethod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.example.toml"


def _build_iso9660_dir_record(name: str, extent_lba: int, data_len: int, is_dir: bool = False) -> bytes:
    """Construct a valid minimal ISO9660 directory record."""
    name_bytes = name.encode("ascii")
    rec_len = 33 + len(name_bytes)
    if rec_len % 2 == 1:
        rec_len += 1  # Pad to even length per ISO9660 spec
    buf = bytearray(rec_len)
    buf[0] = rec_len
    buf[2:6] = extent_lba.to_bytes(4, "little")
    buf[6:10] = extent_lba.to_bytes(4, "big")
    buf[10:14] = data_len.to_bytes(4, "little")
    buf[14:18] = data_len.to_bytes(4, "big")
    buf[25] = 0x02 if is_dir else 0x00
    buf[32] = len(name_bytes)
    buf[33 : 33 + len(name_bytes)] = name_bytes
    return bytes(buf)


def _build_iso9660_image(
    sector_size: int,
    data_offset: int,
    sys_ident: str,
    files: dict[str, bytes],
    dirs: tuple[str, ...] = (),
    total_sectors: int = 32,
) -> bytes:
    """Build a binary-accurate cooked 2048B ISO or 2352B raw Mode 2 XA `.bin` disc image."""
    image = bytearray(total_sectors * sector_size)

    def _write_sector(lba: int, payload: bytes) -> None:
        base = lba * sector_size
        if sector_size == 2352:
            # Write 12-byte CD sync + 4-byte MSF/Mode header
            image[base : base + 12] = b"\x00" + (b"\xff" * 10) + b"\x00"
            image[base + 15] = 2 if data_offset == 24 else 1
        start = base + data_offset
        image[start : start + min(len(payload), 2048)] = payload[:2048]

    root_lba = 18
    next_file_lba = 20
    dir_stream = bytearray()
    # '.' and '..' self/parent entries
    dir_stream.extend(_build_iso9660_dir_record("\x00", root_lba, 2048, is_dir=True))
    dir_stream.extend(_build_iso9660_dir_record("\x01", root_lba, 2048, is_dir=True))

    for dname in dirs:
        dir_stream.extend(_build_iso9660_dir_record(dname, next_file_lba, 2048, is_dir=True))
        next_file_lba += 1

    for fname, content in files.items():
        dir_stream.extend(_build_iso9660_dir_record(fname, next_file_lba, len(content), is_dir=False))
        _write_sector(next_file_lba, content)
        next_file_lba += 1

    _write_sector(root_lba, bytes(dir_stream))

    # Primary Volume Descriptor at Sector 16
    pvd = bytearray(2048)
    pvd[0:6] = b"\x01CD001"
    pvd[6] = 1
    pvd[8:40] = sys_ident.ljust(32)[:32].encode("ascii")
    pvd[40:72] = b"TEST_VOLUME".ljust(32)
    root_rec = _build_iso9660_dir_record("\x00", root_lba, 2048, is_dir=True)
    pvd[156 : 156 + len(root_rec)] = root_rec
    _write_sector(16, bytes(pvd))

    # Volume Descriptor Set Terminator at Sector 17
    term = bytearray(2048)
    term[0:6] = b"\xffCD001"
    term[6] = 1
    _write_sector(17, bytes(term))

    return bytes(image)


class Phase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG_PATH)

    def test_5way_arcade_detection_and_trap_prevention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)

            # 1. CPS1 (`sf2ce.zip`)
            cps1_zip = tdir / "sf2ce.zip"
            with zipfile.ZipFile(cps1_zip, "w") as zf:
                zf.writestr("s92e_23a.8f", b"\xaa" * 512)
                zf.writestr("s92_01.bin", b"\xbb" * 512)
                zf.writestr("s92_09.chr", b"\xcc" * 512)
            info1 = inspect_archive(cps1_zip, self.config)
            self.assertTrue(info1.is_arcade)
            self.assertEqual(info1.arcade_system_id, "cps1")
            with self.assertRaises(RuntimeError):
                extract_archive_to_staging(info1, self.config)

            # 2. CPS2 (`mvsc.zip` with 20-byte `.key`)
            cps2_zip = tdir / "mvsc.zip"
            with zipfile.ZipFile(cps2_zip, "w") as zf:
                zf.writestr("mvsc.key", b"\x11" * 20)
                zf.writestr("mvc.03a", b"\x22" * 1024)
                zf.writestr("mvc.m01", b"\x33" * 512)
            info2 = inspect_archive(cps2_zip, self.config)
            self.assertTrue(info2.is_arcade)
            self.assertEqual(info2.arcade_system_id, "cps2")

            # 3. CPS3 (`sfiii3.zip` with SIMM banks)
            cps3_zip = tdir / "sfiii3.zip"
            with zipfile.ZipFile(cps3_zip, "w") as zf:
                zf.writestr("10", b"\x44" * 1024)
                zf.writestr("20", b"\x55" * 1024)
                zf.writestr("30", b"\x66" * 1024)
            info3 = inspect_archive(cps3_zip, self.config)
            self.assertTrue(info3.is_arcade)
            self.assertEqual(info3.arcade_system_id, "cps3")

            # 4. Neo Geo (`mslugx.zip` with SNK chip names)
            ng_zip = tdir / "Metal_Slug_X_Renamed.zip"
            with zipfile.ZipFile(ng_zip, "w") as zf:
                zf.writestr("250-p1.p1", b"\x77" * 1024)
                zf.writestr("250-c1.c1", b"\x88" * 1024)
                zf.writestr("250-m1.m1", b"\x99" * 512)
                zf.writestr("250-v1.v1", b"\xaa" * 1024)
            info4 = inspect_archive(ng_zip, self.config)
            self.assertTrue(info4.is_arcade)
            self.assertEqual(info4.arcade_system_id, "neogeo")

            # 5. General FBNeo (`tmnt.zip`)
            fb_zip = tdir / "tmnt.zip"
            with zipfile.ZipFile(fb_zip, "w") as zf:
                zf.writestr("963-x23.j17", b"\x01" * 512)
                zf.writestr("963-x24.k17", b"\x02" * 512)
                zf.writestr("963a28.h7", b"\x03" * 512)
            info5 = inspect_archive(fb_zip, self.config)
            self.assertTrue(info5.is_arcade)
            self.assertEqual(info5.arcade_system_id, "fbneo")

            # 6. Console GBA zip MUST NOT trigger Arcade trap
            gba_zip = tdir / "Metroid Fusion (USA).zip"
            with zipfile.ZipFile(gba_zip, "w") as zf:
                zf.writestr("Metroid Fusion (USA).gba", b"\x00" * 2048)
            info6 = inspect_archive(gba_zip, self.config)
            self.assertFalse(info6.is_arcade)

            # 7. Single-file raw `.bin` archive MUST NOT trigger Arcade trap
            single_bin_zip = tdir / "RawGame (USA).zip"
            with zipfile.ZipFile(single_bin_zip, "w") as zf:
                zf.writestr("RawGame (USA).bin", b"\x00" * 4096)
            info7 = inspect_archive(single_bin_zip, self.config)
            self.assertFalse(info7.is_arcade)

    def test_disc_binary_magic_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)

            # GameCube (0xC2339F3D at 0x1C)
            gc_iso = tdir / "Melee.iso"
            gc_buf = bytearray(256)
            gc_buf[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
            gc_iso.write_bytes(gc_buf)
            res_gc = identify_single_file(gc_iso)
            self.assertEqual(res_gc.system_id, "gc")
            self.assertEqual(res_gc.method, DetectionMethod.BINARY_MAGIC)

            # Wii (0x5D1C9EA3 at 0x18)
            wii_iso = tdir / "Galaxy.iso"
            wii_buf = bytearray(256)
            wii_buf[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
            wii_iso.write_bytes(wii_buf)
            res_wii = identify_single_file(wii_iso)
            self.assertEqual(res_wii.system_id, "wii")

            # Sega Saturn (0x10 raw Mode 1 .bin)
            sat_bin = tdir / "PanzerDragoon.bin"
            sat_buf = bytearray(256)
            sat_buf[0x10:0x20] = b"SEGA SEGASATURN "
            sat_bin.write_bytes(sat_buf)
            self.assertEqual(identify_single_file(sat_bin).system_id, "saturn")

            # Sega Dreamcast (0x00)
            dc_bin = tdir / "SoulCalibur.bin"
            dc_buf = bytearray(256)
            dc_buf[0x00:0x10] = b"SEGA SEGAKATANA "
            dc_bin.write_bytes(dc_buf)
            self.assertEqual(identify_single_file(dc_bin).system_id, "dreamcast")

            # Sega CD (0x10)
            scd_bin = tdir / "SonicCD.bin"
            scd_buf = bytearray(256)
            scd_buf[0x10:0x20] = b"SEGADISCSYSTEM  "
            scd_bin.write_bytes(scd_buf)
            self.assertEqual(identify_single_file(scd_bin).system_id, "segacd")

    def test_iso9660_psx_ps2_psp_and_multitrack_cue_grouping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)

            # 1. PS1 multi-track .cue + 2352B Mode 2 XA .bin (`BOOT = cdrom:\...`)
            ps1_dir = tdir / "ps1_bundle"
            ps1_dir.mkdir()
            ps1_bin1 = ps1_dir / "Castlevania (Track 01).bin"
            ps1_bin2 = ps1_dir / "Castlevania (Track 02).bin"
            ps1_cue = ps1_dir / "Castlevania.cue"

            ps1_bin1.write_bytes(
                _build_iso9660_image(
                    sector_size=2352,
                    data_offset=24,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SLUS_000.67;1\r\nTCB = 4\r\n"},
                )
            )
            ps1_bin2.write_bytes(b"\x00" * (2352 * 4))  # Redbook audio track
            ps1_cue.write_text(
                'FILE "Castlevania (Track 01).bin" BINARY\n'
                "  TRACK 01 MODE2/2352\n"
                "    INDEX 01 00:00:00\n"
                'FILE "Castlevania (Track 02).bin" BINARY\n'
                "  TRACK 02 AUDIO\n"
                "    INDEX 01 00:00:00\n",
                encoding="utf-8",
            )

            ps1_results = identify_directory(ps1_dir)
            self.assertEqual(len(ps1_results), 1)
            self.assertEqual(ps1_results[0].system_id, "psx")
            self.assertEqual(ps1_results[0].primary_file.name, "Castlevania.cue")
            self.assertEqual(len(ps1_results[0].companion_files), 2)

            # 2. PS2 DVD .iso (`BOOT2 = cdrom0:\...`)
            ps2_iso = tdir / "ShadowOfTheColossus.iso"
            ps2_iso.write_bytes(
                _build_iso9660_image(
                    sector_size=2048,
                    data_offset=0,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF;1": b"BOOT2 = cdrom0:\\SCUS_974.72;1\r\nVER = 1.00\r\n"},
                )
            )
            res_ps2 = identify_single_file(ps2_iso, logical_size=2_500_000_000)
            self.assertEqual(res_ps2.system_id, "ps2")
            self.assertEqual(res_ps2.disc_media_type, "dvd")

            # 3. PSP .iso (`PSP_GAME` directory in ISO9660 root)
            psp_iso = tdir / "OutRun2006.iso"
            psp_iso.write_bytes(
                _build_iso9660_image(
                    sector_size=2048,
                    data_offset=0,
                    sys_ident="PSP GAME",
                    files={"UMD_DATA.BIN;1": b"ULES-00262|0001"},
                    dirs=("PSP_GAME",),
                )
            )
            res_psp = identify_single_file(psp_iso)
            self.assertEqual(res_psp.system_id, "psp")

    def test_3ds_ncch_encryption_and_switch_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)

            # Decrypted .3ds (NCSD at 0x100, part0 offset 0x20 media units = 0x4000, NCCH flags[7]=0x04)
            dec_3ds = tdir / "ZeldaOOT.3ds"
            buf_dec = bytearray(0x4200)
            buf_dec[0x100:0x104] = b"NCSD"
            buf_dec[0x120:0x124] = (0x20).to_bytes(4, "little")
            buf_dec[0x4100:0x4104] = b"NCCH"
            buf_dec[0x418F] = 0x04  # NoCrypto bit set
            dec_3ds.write_bytes(buf_dec)
            r_dec = identify_single_file(dec_3ds)
            self.assertEqual(r_dec.system_id, "3ds")
            self.assertFalse(r_dec.is_encrypted_3ds)

            # Encrypted .3ds (NoCrypto bit = 0)
            enc_3ds = tdir / "EncryptedGame.3ds"
            buf_enc = bytearray(buf_dec)
            buf_enc[0x418F] = 0x00
            enc_3ds.write_bytes(buf_enc)
            r_enc = identify_single_file(enc_3ds)
            self.assertEqual(r_enc.system_id, "3ds")
            self.assertTrue(r_enc.is_encrypted_3ds)

            # Switch Multi-File Bundle (Base + Update + DLC)
            sw_dir = tdir / "SwitchBundle"
            sw_dir.mkdir()
            (sw_dir / "Game [0100AAA000000000][v0].nsp").write_bytes(b"PFS0")
            (sw_dir / "Game [0100AAA000000800][v65536].nsp").write_bytes(b"PFS0")
            (sw_dir / "Game [0100AAA000000001][v0].nsz").write_bytes(b"NSZ!")

            sw_results = identify_directory(sw_dir)
            self.assertEqual(len(sw_results), 1)
            self.assertEqual(sw_results[0].system_id, "switch")
            self.assertTrue(sw_results[0].is_multifile_switch)
            self.assertTrue(sw_results[0].is_compressed_switch)
            self.assertEqual(len(sw_results[0].companion_files), 2)

    def test_7z_archive_streaming_header_inspect(self) -> None:
        seven_zip = self.config.binaries.seven_zip
        if seven_zip is None:
            self.skipTest("7z binary not available")

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            iso_path = tdir / "GodHand [USA].iso"
            iso_path.write_bytes(
                _build_iso9660_image(
                    sector_size=2048,
                    data_offset=0,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF;1": b"BOOT2 = cdrom0:\\SLUS_215.03;1\r\n"},
                )
            )
            arc_7z = tdir / "GodHand [USA].7z"
            subprocess.run(
                [str(seven_zip), "a", "-y", str(arc_7z), str(iso_path)],
                check=True,
                capture_output=True,
            )

            # Test via `main.main(["inspect", ...])` end-to-end!
            from main import main as cli_main

            rc = cli_main(["--config", str(CONFIG_PATH), "inspect", str(arc_7z)])
            self.assertEqual(rc, 0)
            # Ensure temp `_inspect_*` folder was cleaned up inside `.staging`
            leftovers = list(self.config.paths.staging_dir.glob("_inspect_*"))
            self.assertEqual(leftovers, [])

    def test_precompressed_chd_binary_sniffing(self) -> None:
        chdman = self.config.binaries.chdman
        if chdman is None:
            self.skipTest("chdman binary not available")

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # Create a real PS1 .cue/.bin -> compress to .chd with `chdman createcd` -> sniff .chd!
            bin_path = tdir / "RidgeRacer.bin"
            cue_path = tdir / "RidgeRacer.cue"
            chd_path = tdir / "RidgeRacer.chd"
            bin_path.write_bytes(
                _build_iso9660_image(
                    sector_size=2352,
                    data_offset=24,
                    sys_ident="PLAYSTATION",
                    files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SCUS_943.00;1\r\n"},
                    total_sectors=64,
                )
            )
            cue_path.write_text(
                'FILE "RidgeRacer.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            subprocess.run(
                [str(chdman), "createcd", "-i", str(cue_path), "-o", str(chd_path), "-f"],
                check=True,
                capture_output=True,
            )
            res_chd = identify_single_file(chd_path, chdman_bin=chdman)
            self.assertEqual(res_chd.system_id, "psx")
            self.assertIn("Inside `.chd`", res_chd.reason)

    def test_seven_zip_rar_capability_check(self) -> None:
        cfg = load_config(CONFIG_PATH)
        self.assertIsNotNone(cfg.binaries.seven_zip)
        from src.config_loader import check_seven_zip_supports_rar

        # On Fedora's distro package, RAR support is stripped
        has_rar = check_seven_zip_supports_rar(cfg.binaries.seven_zip)
        self.assertIsInstance(has_rar, bool)

    def test_rar_inspection_helpful_error_when_rar_unsupported(self) -> None:
        cfg = load_config(CONFIG_PATH)
        from src.archive_handler import ArchiveError, list_archive_entries
        from src.config_loader import check_seven_zip_supports_rar

        rar_path = PROJECT_ROOT / "drop" / "Super Metroid (axekin.com).rar"
        if not rar_path.is_file():
            self.skipTest("RAR test file not found in drop/")

        if not check_seven_zip_supports_rar(cfg.binaries.seven_zip):
            with self.assertRaises(ArchiveError) as ctx:
                list_archive_entries(rar_path, cfg.binaries.seven_zip)
            self.assertIn("lacks RAR decompression support", str(ctx.exception))
            self.assertIn("7zz", str(ctx.exception))


    def test_single_quoted_cue_with_desynced_watermark_tracks(self) -> None:
        from src.identifier import identify_directory, parse_cue_tracks

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            cue_file = tdir / "Pac-Man World 20th Anniversary (axekin.com).cue"
            cue_file.write_text(
                "FILE 'Pac-Man World 20th Anniversary (axekin.com) (Track 01).bin' BINARY\n"
                "  TRACK 01 MODE2/2352\n"
                "    INDEX 01 00:00:00\n"
                "FILE 'Pac-Man World 20th Anniversary (axekin.com) (Track 02).bin' BINARY\n"
                "  TRACK 02 AUDIO\n"
                "    INDEX 01 00:02:00\n",
                encoding="utf-8",
            )
            t1 = tdir / "Pac-Man World 20th Anniversary  (Track 01) (axekin.com).bin"
            t2 = tdir / "Pac-Man World 20th Anniversary  (Track 02) (axekin.com).bin"
            ps1_img = _build_iso9660_image(
                sector_size=2352,
                data_offset=24,
                sys_ident="PLAYSTATION",
                files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SLUS_004.33;1\r\n"},
                total_sectors=64,
            )
            t1.write_bytes(ps1_img)
            t2.write_bytes(b"\x00" * 4096)

            tracks = parse_cue_tracks(cue_file)
            self.assertEqual(len(tracks), 2)
            self.assertEqual(tracks[0], t1.resolve())
            self.assertEqual(tracks[1], t2.resolve())

            results = identify_directory(tdir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].system_id, "psx")
            self.assertEqual(results[0].primary_file, cue_file.resolve())
            self.assertEqual(len(results[0].companion_files), 2)

    def test_zerokey_3ds_inside_archive_inspect_plans_decryption_without_ctr_keys(self) -> None:
        from dataclasses import replace
        import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # Isolated config with NO ctr_keys and config_path in temp dir (so tools/3ds fallback is empty)
            cfg_no_keys = replace(
                self.config,
                config_path=tdir / "config.toml",
                paths=replace(self.config.paths, staging_dir=tdir / ".staging"),
                binaries=replace(self.config.binaries, ctr_keys=None),
            )
            cfg_no_keys.paths.staging_dir.mkdir(parents=True, exist_ok=True)

            # Build a ZeroKey-encrypted .3ds (`flags[7] = 0x01` FixedCryptoKey, PartitionID bit 0x10<<32 clear)
            buf_zk = bytearray(0x4200)
            buf_zk[0x100:0x104] = b"NCSD"
            buf_zk[0x120:0x124] = (0x20).to_bytes(4, "little")
            buf_zk[0x124:0x128] = (0x01).to_bytes(4, "little")
            buf_zk[0x4100:0x4104] = b"NCCH"
            buf_zk[0x4108:0x4110] = (0x0004000000030800).to_bytes(8, "little")
            buf_zk[0x418F] = 0x01  # FixedCryptoKey (ZeroKey), NoCrypto NOT set

            zk_zip = tdir / "ZeroKeyGame.zip"
            with zipfile.ZipFile(zk_zip, "w") as zf:
                zf.writestr("ZeroKeyGame.3ds", bytes(buf_zk))

            _, items = cli_main._inspect_single_target(zk_zip, cfg_no_keys)
            self.assertEqual(len(items), 1)
            res, from_arc, arc_ext = items[0]
            self.assertTrue(from_arc)
            self.assertTrue(res.is_encrypted_3ds)
            self.assertTrue(res.is_zerokey_3ds)
            # Preview staging dir is already deleted and primary_file is rebound to display path:
            self.assertFalse(res.primary_file.exists())

            planned = cli_main._describe_planned_action(res, cfg_no_keys, from_arc, arc_ext)
            self.assertIn("Decrypt .3ds -> .cci", planned)
            self.assertNotIn("Quarantine", planned)

    def test_multitrack_cd_preview_touches_secondary_audio_tracks_without_7z_spawns(self) -> None:
        from dataclasses import replace
        import shutil
        from src.archive_handler import extract_header_preview_to_staging

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            cfg = replace(
                self.config,
                paths=replace(self.config.paths, staging_dir=tdir / ".staging"),
            )
            cfg.paths.staging_dir.mkdir(parents=True, exist_ok=True)

            ps1_img = _build_iso9660_image(
                sector_size=2352,
                data_offset=24,
                sys_ident="PLAYSTATION",
                files={"SYSTEM.CNF;1": b"BOOT = cdrom:\\SLUS_000.67;1\r\n"},
                total_sectors=64,
            )
            cd_zip = tdir / "Castlevania SOTN.zip"
            with zipfile.ZipFile(cd_zip, "w") as zf:
                zf.writestr(
                    "Castlevania.cue",
                    'FILE "Castlevania (Track 01).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                    'FILE "Castlevania (Track 02).bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 01 00:00:00\n'
                    'FILE "Castlevania (Track 03).bin" BINARY\n  TRACK 03 AUDIO\n    INDEX 01 00:00:00\n',
                )
                zf.writestr("Castlevania (Track 01).bin", ps1_img)
                zf.writestr("Castlevania (Track 02).bin", b"\xaa" * 32768)
                zf.writestr("Castlevania (Track 03).bin", b"\xbb" * 65536)

            arc_info = inspect_archive(cd_zip, cfg)
            preview_dir, logical_sizes = extract_header_preview_to_staging(arc_info, cfg)
            try:
                t1_preview = preview_dir / "Castlevania (Track 01).bin"
                t2_preview = preview_dir / "Castlevania (Track 02).bin"
                t3_preview = preview_dir / "Castlevania (Track 03).bin"
                self.assertGreater(t1_preview.stat().st_size, 0)
                # Secondary audio tracks (Track 02..99) must be 0-byte placeholders in preview staging
                self.assertEqual(t2_preview.stat().st_size, 0)
                self.assertEqual(t3_preview.stat().st_size, 0)
                self.assertEqual(logical_sizes[t2_preview.resolve()], 32768)
                self.assertEqual(logical_sizes[t3_preview.resolve()], 65536)

                results = identify_directory(preview_dir, logical_sizes=logical_sizes)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].system_id, "psx")
                self.assertEqual(len(results[0].companion_files), 3)
            finally:
                shutil.rmtree(preview_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
