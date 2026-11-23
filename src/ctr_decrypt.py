"""Pure-Python streaming Nintendo 3DS NCSD/NCCH (.3ds -> decrypted .cci) decryptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil
from typing import BinaryIO

from src.constants import DEFAULT_CTR_KEY_RELPATHS
from src.models import PipelineConfig

_MASK_128 = (1 << 128) - 1
_DEFAULT_GENERATOR = 0x1FF9E9AAC5FE0408024591DC5D52768A
_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB (multiple of 16 bytes for AES-CTR)

_SEC_CRYPTO_KEYSLOTS: dict[int, int] = {
    0x00: 0x2C,  # Original (< 7.x)
    0x01: 0x25,  # 7.x
    0x0A: 0x18,  # New3DS 9.3 (Secure3)
    0x0B: 0x1B,  # New3DS 9.6 (Secure4)
}

_SLOT_KEYX_RE = re.compile(r"^slot(?:0x)?([0-9a-f]{2})keyx$", re.IGNORECASE)
_SLOT_KEY_RE = re.compile(r"^slot(?:0x)?([0-9a-f]{2})key$", re.IGNORECASE)


class CtrDecryptionError(RuntimeError):
    """Raised when 3DS keys are missing/invalid or NCCH decryption fails verification."""


def _import_aes():
    """Import AES from pycryptodome (`Crypto`) or pycryptodomex (`Cryptodome`)."""
    try:
        from Crypto.Cipher import AES  # type: ignore[import-not-found]
        return AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES  # type: ignore[import-not-found]
            return AES
        except ImportError as exc:
            raise CtrDecryptionError(
                "3DS decryption requires `pycryptodome` (`pip install -r requirements.txt`)."
            ) from exc


def _rol128(val: int, bits: int) -> int:
    bits %= 128
    return ((val << bits) | (val >> (128 - bits))) & _MASK_128


def scramble_3ds_key(
    key_x: bytes,
    key_y: bytes,
    generator: int = _DEFAULT_GENERATOR,
) -> bytes:
    """Derive a 128-bit 3DS AES NormalKey from `KeyX`, `KeyY`, and the hardware generator."""
    kx = int.from_bytes(key_x, "big")
    ky = int.from_bytes(key_y, "big")
    normal = _rol128(((_rol128(kx, 2) ^ ky) + generator) & _MASK_128, 87)
    return normal.to_bytes(16, "big")


@dataclass(slots=True)
class CtrKeyStore:
    """Loaded 3DS AES keyslots (`aes_keys.txt` or `boot9.bin`)."""

    source_path: Path
    generator: int = _DEFAULT_GENERATOR
    key_x: dict[int, bytes] = field(default_factory=dict)
    normal_key: dict[int, bytes] = field(default_factory=dict)

    def derive_key(self, slot: int, key_y: bytes) -> bytes:
        if slot in self.normal_key:
            return self.normal_key[slot]
        if slot in self.key_x:
            return scramble_3ds_key(self.key_x[slot], key_y, self.generator)
        raise CtrDecryptionError(
            f"Missing 3DS AES keyslot `0x{slot:02X}` (`slot0x{slot:02X}KeyX`) in `{self.source_path.name}`."
        )


def _load_keys_from_boot9(boot9_path: Path) -> CtrKeyStore:
    """Load retail 3DS KeyX slots from an ARM9 `boot9.bin` dump via `pyctr`."""
    try:
        from pyctr.crypto import CryptoEngine  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CtrDecryptionError(
            f"Reading `{boot9_path.name}` requires `pyctr` (`pip install pyctr`), "
            "or provide a text `aes_keys.txt` file instead."
        ) from exc

    try:
        engine = CryptoEngine(boot9=str(boot9_path))
    except Exception as exc:
        raise CtrDecryptionError(f"Failed loading 3DS keys from `{boot9_path.name}`: {exc}") from exc

    store = CtrKeyStore(source_path=boot9_path)
    raw_kx = getattr(engine, "key_x", {}) or {}
    for slot_key, val in raw_kx.items():
        slot_int = int(slot_key)
        if isinstance(val, int):
            store.key_x[slot_int] = val.to_bytes(16, "big")
        elif isinstance(val, (bytes, bytearray)) and len(val) == 16:
            store.key_x[slot_int] = bytes(val)

    if 0x2C not in store.key_x:
        raise CtrDecryptionError(
            f"`{boot9_path.name}` did not yield retail NCCH keyslot 0x2C (invalid or devkit boot9?)."
        )
    return store


def _load_keys_from_text(keys_path: Path) -> CtrKeyStore:
    """Parse Citra/Azahar `aes_keys.txt` (`slot0x2CKeyX=...`, `generator=...`)."""
    store = CtrKeyStore(source_path=keys_path)
    try:
        lines = keys_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise CtrDecryptionError(f"Unable to read `{keys_path}`: {exc}") from exc

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = (part.strip() for part in line.split("=", 1))
        hex_clean = re.sub(r"\s+", "", v)
        if len(hex_clean) != 32:
            continue
        try:
            key_bytes = bytes.fromhex(hex_clean)
        except ValueError:
            continue

        k_low = k.lower()
        if k_low == "generator":
            store.generator = int.from_bytes(key_bytes, "big")
            continue

        m_kx = _SLOT_KEYX_RE.match(k_low)
        if m_kx:
            store.key_x[int(m_kx.group(1), 16)] = key_bytes
            continue

        m_k = _SLOT_KEY_RE.match(k_low)
        if m_k:
            store.normal_key[int(m_k.group(1), 16)] = key_bytes

    if not store.key_x and not store.normal_key:
        raise CtrDecryptionError(
            f"No valid 3DS AES keyslots found in `{keys_path}` (expected `slot0x2CKeyX=...`)."
        )
    return store


def load_ctr_keys(key_path: Path) -> CtrKeyStore:
    """Load 3DS AES keys from either `aes_keys.txt` or `boot9.bin`."""
    if not key_path.is_file():
        raise CtrDecryptionError(f"3DS key file does not exist: {key_path}")
    if key_path.suffix.lower() == ".bin" or key_path.stat().st_size in (32768, 65536):
        return _load_keys_from_boot9(key_path)
    return _load_keys_from_text(key_path)


def inspect_3ds_header(file_path: Path) -> tuple[bool, bool, str]:
    """
    Inspect a Nintendo 3DS NCSD/NCCH binary header.

    Returns `(is_encrypted, is_zerokey, detail_reason)`.
    """
    try:
        with file_path.open("rb") as fh:
            head = fh.read(0x200)
            if len(head) < 0x190:
                return False, False, "3DS extension match (header shorter than NCSD)"

            # Case 1: NCSD cartridge container (`NCSD` at 0x100)
            if head[0x100:0x104] == b"NCSD":
                part0_mu = int.from_bytes(head[0x120:0x124], "little")
                fh.seek(part0_mu * 0x200)
                ncch0 = fh.read(0x200)
                if len(ncch0) < 0x190 or ncch0[0x100:0x104] != b"NCCH":
                    return (
                        False,
                        False,
                        "Verified NCSD header (Partition 0 NCCH unreadable or non-standard)",
                    )

                partitions: list[int] = [part0_mu * 0x200]
                for idx in range(1, 8):
                    off_mu = int.from_bytes(head[0x120 + idx * 8 : 0x124 + idx * 8], "little")
                    sz_mu = int.from_bytes(head[0x124 + idx * 8 : 0x128 + idx * 8], "little")
                    if sz_mu > 0 and (off_mu * 0x200) not in partitions:
                        partitions.append(off_mu * 0x200)

                found_encrypted = False
                all_zerokey = True
                for off in partitions:
                    fh.seek(off)
                    hdr = fh.read(0x200)
                    if len(hdr) < 0x190 or hdr[0x100:0x104] != b"NCCH":
                        continue
                    flags7 = hdr[0x18F]
                    if flags7 & 0x04:  # NoCrypto
                        continue
                    found_encrypted = True
                    pid = int.from_bytes(hdr[0x108:0x110], "little")
                    if not ((flags7 & 0x01) and (pid & (0x10 << 32)) == 0):
                        all_zerokey = False

                if not found_encrypted:
                    return False, False, "Verified NCSD+NCCH header (`NoCrypto` bit set — Decrypted)"
                if all_zerokey:
                    return True, True, "Verified NCSD+NCCH header (`FixedCryptoKey` ZeroKey — Encrypted)"
                return True, False, "Verified NCSD+NCCH header (`NoCrypto` bit NOT set — Encrypted)"

            # Case 2: Standalone NCCH container (`NCCH` at 0x100)
            if head[0x100:0x104] == b"NCCH":
                flags7 = head[0x18F]
                no_crypto = bool(flags7 & 0x04)
                if no_crypto:
                    return False, False, "Verified standalone NCCH header (Decrypted)"
                pid = int.from_bytes(head[0x108:0x110], "little")
                is_zk = bool((flags7 & 0x01) and (pid & (0x10 << 32)) == 0)
                return True, is_zk, "Verified standalone NCCH header (Encrypted)"
    except OSError:
        pass
    return False, False, "Matched 3DS extension"


def _uses_zerokey_only(file_path: Path) -> bool:
    """Return True if all encrypted NCCH partitions in `file_path` use ZeroKey (`FixedCryptoKey`)."""
    is_enc, is_zk, _ = inspect_3ds_header(file_path)
    return is_enc and is_zk


def _resolve_ctr_keys_path(config: PipelineConfig) -> Path | None:
    """Resolve `ctr_keys`, checking `tools/3ds/` dynamically if added after watcher startup."""
    keys = config.binaries.ctr_keys
    if keys is not None and keys.is_file():
        return keys
    project_root = config.config_path.parent
    for rel in DEFAULT_CTR_KEY_RELPATHS:
        cand = (project_root / rel).resolve()
        if cand.is_file():
            return cand
    return None


def can_decrypt_3ds(file_path: Path, config: PipelineConfig) -> bool:
    """Return True if `file_path` can be decrypted (either `ctr_keys` exists or ZeroKey-only)."""
    if _resolve_ctr_keys_path(config) is not None:
        return True
    return _uses_zerokey_only(file_path)


def _copy_span(fin: BinaryIO, fout: BinaryIO, length: int) -> None:
    remaining = length
    while remaining > 0:
        buf = fin.read(min(_CHUNK_SIZE, remaining))
        if not buf:
            raise CtrDecryptionError("Unexpected EOF while copying 3DS container bytes.")
        fout.write(buf)
        remaining -= len(buf)


def _decrypt_span(fin: BinaryIO, fout: BinaryIO, length: int, key: bytes, iv_int: int, aes_mod) -> None:
    cipher = aes_mod.new(key, aes_mod.MODE_CTR, nonce=b"", initial_value=iv_int.to_bytes(16, "big"))
    remaining = length
    while remaining > 0:
        buf = fin.read(min(_CHUNK_SIZE, remaining))
        if not buf:
            raise CtrDecryptionError("Unexpected EOF while decrypting 3DS NCCH section.")
        fout.write(cipher.decrypt(buf))
        remaining -= len(buf)


def _ncch_section_iv(hdr: bytes, ncch_ver: int, section_type: int, rel_offset: int) -> int:
    """Compute initial 128-bit big-endian CTR counter for an NCCH section."""
    if ncch_ver == 1:
        base_iv = hdr[0x108:0x110] + b"\x00\x00\x00\x00" + rel_offset.to_bytes(4, "big")
        return int.from_bytes(base_iv, "big")
    # NCCH v0 / v2: reversed 8-byte PartitionID + type byte (1=ExHeader, 2=ExeFS, 3=RomFS) + 7 zeros
    base_iv = hdr[0x108:0x110][::-1] + bytes([section_type]) + (b"\x00" * 7)
    return int.from_bytes(base_iv, "big")


def _parse_exefs_code_span(exefs_hdr: bytes) -> tuple[int, int] | None:
    """Locate `.code` offset/size (relative to ExeFS start) inside decrypted 0x200 ExeFS header."""
    for idx in range(10):
        entry = exefs_hdr[idx * 16 : (idx + 1) * 16]
        name = entry[0:8].split(b"\x00", 1)[0]
        if name == b".code":
            rel_off = int.from_bytes(entry[8:12], "little")
            size = int.from_bytes(entry[12:16], "little")
            if size == 0:
                return None
            # ExeFS files start after the 0x200-byte header and are padded to 0x200 units
            start = 0x200 + rel_off
            padded_len = ((size + 0x1FF) // 0x200) * 0x200
            return start, start + padded_len
    return None


def _resolve_partition_keys(hdr: bytes, keys: CtrKeyStore | None) -> tuple[bytes, bytes]:
    """Return `(primary_key, secondary_key)` for an encrypted NCCH partition."""
    flags3 = hdr[0x18B]
    flags7 = hdr[0x18F]
    pid = int.from_bytes(hdr[0x108:0x110], "little")

    if flags7 & 0x20:
        raise CtrDecryptionError(
            "3DS SeedCrypto (`flags[7] & 0x20`) is not supported for `.3ds` decryption."
        )

    if flags7 & 0x01:  # FixedCryptoKey
        if (pid & (0x10 << 32)) == 0:
            zero = b"\x00" * 16
            return zero, zero
        raise CtrDecryptionError("System FixedCryptoKey (non-zero) is not supported.")

    if keys is None:
        raise CtrDecryptionError(
            "Encrypted 3DS ROM requires `aes_keys.txt` or `boot9.bin` in `tools/3ds/`."
        )

    key_y = hdr[0x000:0x010]
    primary_key = keys.derive_key(0x2C, key_y)
    if flags3 == 0x00:
        return primary_key, primary_key

    sec_slot = _SEC_CRYPTO_KEYSLOTS.get(flags3)
    if sec_slot is None:
        raise CtrDecryptionError(f"Unsupported 3DS NCCH secondary crypto flag: 0x{flags3:02X}.")
    secondary_key = keys.derive_key(sec_slot, key_y)
    return primary_key, secondary_key


def decrypt_3ds_to_cci(
    input_path: Path,
    job_dir: Path,
    config: PipelineConfig,
    clean_stem: str | None = None,
) -> Path:
    """
    Decrypt an encrypted `.3ds` (NCSD/NCCH) ROM into a staging `.cci` ready for Azahar.

    Streams in 4 MiB chunks, patches `NoCrypto` (`0x04`) in each NCCH header, and verifies
    RomFS `IVFC` magic bytes after decryption so bad/mismatched keys fail loudly.
    """
    aes_mod = _import_aes()
    key_path = _resolve_ctr_keys_path(config)
    keys = load_ctr_keys(key_path) if key_path is not None else None

    file_size = input_path.stat().st_size
    with input_path.open("rb") as fh:
        head = fh.read(0x200)
        if len(head) < 0x200:
            raise CtrDecryptionError(f"File too small to be a 3DS image: {input_path.name}")

        raw_parts: list[tuple[int, int]] = []
        if head[0x100:0x104] == b"NCSD":
            for idx in range(8):
                off_mu = int.from_bytes(head[0x120 + idx * 8 : 0x124 + idx * 8], "little")
                sz_mu = int.from_bytes(head[0x124 + idx * 8 : 0x128 + idx * 8], "little")
                if sz_mu > 0:
                    off_b = off_mu * 0x200
                    sz_b = sz_mu * 0x200
                    if off_b + 0x200 <= file_size:
                        raw_parts.append((off_b, min(sz_b, file_size - off_b)))
        elif head[0x100:0x104] == b"NCCH":
            raw_parts.append((0, file_size))
        else:
            raise CtrDecryptionError(f"Unsupported 3DS container (expected NCSD or NCCH): {input_path.name}")

    raw_parts.sort(key=lambda item: item[0])

    job_dir.mkdir(parents=True, exist_ok=True)
    stem = clean_stem or input_path.stem
    out_cci = job_dir / f"{stem}.cci"
    if out_cci.resolve() == input_path.resolve():
        out_cci = job_dir / f"{stem}_decrypted.cci"

    romfs_verify_offsets: list[int] = []
    cursor = 0

    try:
        with input_path.open("rb") as fin, out_cci.open("wb") as fout:
            for part_off, part_len in raw_parts:
                if part_off > cursor:
                    _copy_span(fin, fout, part_off - cursor)
                    cursor = part_off
                elif part_off < cursor:
                    continue

                hdr = bytearray(fin.read(0x200))
                if len(hdr) < 0x200 or hdr[0x100:0x104] != b"NCCH":
                    fout.write(hdr)
                    cursor += len(hdr)
                    continue

                flags7 = hdr[0x18F]
                if flags7 & 0x04:  # Partition already decrypted
                    fout.write(hdr)
                    cursor += 0x200
                    _copy_span(fin, fout, part_len - 0x200)
                    cursor += part_len - 0x200
                    continue

                primary_key, secondary_key = _resolve_partition_keys(bytes(hdr), keys)
                ncch_ver = int.from_bytes(hdr[0x112:0x114], "little")
                # NCCH offsets and sizes are always in 0x200-byte media units (flags[5] is alignment only)
                unit_size = 0x200
                exhdr_size = int.from_bytes(hdr[0x180:0x184], "little")
                exefs_off = int.from_bytes(hdr[0x1A0:0x1A4], "little") * unit_size
                exefs_len = int.from_bytes(hdr[0x1A4:0x1A8], "little") * unit_size
                romfs_off = int.from_bytes(hdr[0x1B0:0x1B4], "little") * unit_size
                romfs_len = int.from_bytes(hdr[0x1B4:0x1B8], "little") * unit_size

                # Patch NCCH header: clear secondary crypto byte & Fixed/Seed flags, set NoCrypto (0x04)
                orig_hdr = bytes(hdr)
                hdr[0x18B] = 0x00
                hdr[0x18F] = (flags7 & ~0x21) | 0x04
                fout.write(hdr)
                cursor += 0x200
                part_cursor = 0x200

                # 1. Extended Header (always 0x800 bytes on disk when exhdr_size > 0)
                if exhdr_size > 0 and part_cursor + 0x800 <= part_len:
                    iv_exh = _ncch_section_iv(orig_hdr, ncch_ver, 1, part_cursor)
                    _decrypt_span(fin, fout, 0x800, primary_key, iv_exh, aes_mod)
                    cursor += 0x800
                    part_cursor += 0x800

                # 2. ExeFS
                if exefs_len > 0 and exefs_off >= part_cursor and exefs_off + exefs_len <= part_len:
                    if exefs_off > part_cursor:
                        gap = exefs_off - part_cursor
                        _copy_span(fin, fout, gap)
                        cursor += gap
                        part_cursor += gap

                    exefs_iv = _ncch_section_iv(orig_hdr, ncch_ver, 2, exefs_off)
                    if primary_key == secondary_key:
                        _decrypt_span(fin, fout, exefs_len, primary_key, exefs_iv, aes_mod)
                        cursor += exefs_len
                        part_cursor += exefs_len
                    else:
                        # 7.x+ split ExeFS: header & icons use primary_key; `.code` uses secondary_key
                        raw_exefs_hdr = fin.read(0x200)
                        if len(raw_exefs_hdr) < 0x200:
                            raise CtrDecryptionError("Unexpected EOF reading 3DS ExeFS header.")
                        hdr_cipher = aes_mod.new(
                            primary_key,
                            aes_mod.MODE_CTR,
                            nonce=b"",
                            initial_value=exefs_iv.to_bytes(16, "big"),
                        )
                        dec_exefs_hdr = hdr_cipher.decrypt(raw_exefs_hdr)
                        fout.write(dec_exefs_hdr)
                        cursor += 0x200
                        part_cursor += 0x200

                        exefs_pos = 0x200
                        code_span = _parse_exefs_code_span(dec_exefs_hdr)
                        if code_span is not None and code_span[1] <= exefs_len:
                            c_start, c_end = code_span
                            if c_start > exefs_pos:
                                _decrypt_span(
                                    fin, fout, c_start - exefs_pos,
                                    primary_key, exefs_iv + (exefs_pos // 16), aes_mod,
                                )
                                exefs_pos = c_start
                            _decrypt_span(
                                fin, fout, c_end - c_start,
                                secondary_key, exefs_iv + (c_start // 16), aes_mod,
                            )
                            exefs_pos = c_end
                        if exefs_pos < exefs_len:
                            _decrypt_span(
                                fin, fout, exefs_len - exefs_pos,
                                primary_key, exefs_iv + (exefs_pos // 16), aes_mod,
                            )
                        rem_exefs = exefs_len - 0x200
                        cursor += rem_exefs
                        part_cursor += rem_exefs

                # 3. RomFS
                if romfs_len > 0 and romfs_off >= part_cursor and romfs_off < part_len:
                    if romfs_off > part_cursor:
                        gap = romfs_off - part_cursor
                        _copy_span(fin, fout, gap)
                        cursor += gap
                        part_cursor += gap

                    actual_romfs_len = min(romfs_len, part_len - romfs_off)
                    romfs_verify_offsets.append(part_off + romfs_off)
                    romfs_iv = _ncch_section_iv(orig_hdr, ncch_ver, 3, romfs_off)
                    _decrypt_span(fin, fout, actual_romfs_len, secondary_key, romfs_iv, aes_mod)
                    cursor += actual_romfs_len
                    part_cursor += actual_romfs_len

                # Trailing partition padding
                if part_cursor < part_len:
                    rem = part_len - part_cursor
                    _copy_span(fin, fout, rem)
                    cursor += rem

            # Trailing NCSD padding after last partition
            if cursor < file_size:
                _copy_span(fin, fout, file_size - cursor)

        # Cryptographic verification: every newly decrypted RomFS must begin with `IVFC`
        if romfs_verify_offsets:
            with out_cci.open("rb") as vfh:
                for romfs_abs_off in romfs_verify_offsets:
                    vfh.seek(romfs_abs_off)
                    magic = vfh.read(4)
                    if magic != b"IVFC":
                        raise CtrDecryptionError(
                            "3DS decryption verification failed (RomFS `IVFC` magic mismatch — check your 3DS keys)."
                        )

        return out_cci
    except Exception:
        out_cci.unlink(missing_ok=True)
        raise
