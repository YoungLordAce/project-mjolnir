"""Syncthing-safe atomic NAS routing, POSIX .m3u generation, quarantine, and job orchestration."""

from __future__ import annotations

import ctypes
from dataclasses import replace as dataclass_replace
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import uuid

from src.archive_handler import (
    StagingJobDir,
    check_staging_space,
    extract_archive_to_staging,
    extract_header_preview_to_staging,
    inspect_archive,
    is_archive,
)
from src.config_loader import ensure_local_directories, try_mount_smb_if_needed
from src.constants import (
    IGNORED_FILENAMES,
    IGNORED_SIDECAR_EXTENSIONS,
    is_syncthing_temp as _is_syncthing_temp,
)
from src.converter import build_dry_run_artifact, convert_identified_item, merge_psx_multidisc_pbp
from src.identifier import (
    COMPRESSED_SWITCH_EXTENSIONS,
    SWITCH_EXTENSIONS,
    TruncatedPreviewError,
    UNAMBIGUOUS_EXTENSION_MAP,
    _find_pkg_license_companions,
    identify_directory,
    identify_single_file,
    parse_cue_tracks,
    parse_gdi_tracks,
    parse_multidisc_info,
    sanitize_rom_stem,
)
from src.models import (
    ArchiveInspection,
    ArchivePolicy,
    ConversionArtifact,
    DetectionMethod,
    IdentificationResult,
    PipelineConfig,
    PostProcessAction,
    RoutingOutcome,
)
from src.ctr_decrypt import can_decrypt_3ds
from src.switch_bundler import (
    SwitchContentRole,
    classify_switch_title_id,
    extract_switch_title_ids_from_file,
    find_nas_switch_family_files,
    group_candidates_with_switch_families,
    has_explicit_addon_only_tag,
    probe_switch_source,
    switch_package_sort_key,
)


class RoutingError(RuntimeError):
    """Raised when NAS destination transfer, verification, or atomic rename fails."""


M3U_CAPABLE_SYSTEMS: frozenset[str] = frozenset({
    "psx", "ps2", "gc", "wii", "saturn", "dreamcast", "segacd", "pcenginecd",
})

DISC_IMAGE_EXTS_FOR_M3U: frozenset[str] = frozenset({
    ".chd", ".rvz", ".iso", ".cue", ".gdi", ".ciso", ".gcz", ".pbp",
})

COPY_BUFFER_BYTES = 1024 * 1024  # 1 MiB chunks for optimal SMB/LAN throughput


def _clear_windows_hidden_attribute(file_path: Path) -> None:
    """
    Ensure `FILE_ATTRIBUTE_HIDDEN` (`0x02`) is cleared on Windows / Samba SMB shares.

    Linux Samba servers with `hide dot files = yes` + `store dos attributes = yes` stamp
    `FILE_ATTRIBUTE_HIDDEN` into `user.DOSATTRIB` if a file ever starts with `.`, and
    preserve that xattr across `os.replace()`. Using `~syncthing~*.tmp` avoids dotfiles
    entirely, and this call guarantees the final file is never marked hidden.
    """
    if sys.platform != "win32":
        return
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_attrs = kernel32.GetFileAttributesW
        get_attrs.argtypes = [wintypes.LPCWSTR]
        get_attrs.restype = wintypes.DWORD

        set_attrs = kernel32.SetFileAttributesW
        set_attrs.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        set_attrs.restype = wintypes.BOOL

        attrs = get_attrs(str(file_path))
        invalid_attrs = 0xFFFFFFFF
        hidden_bit = 0x02
        normal_bit = 0x80
        if attrs != invalid_attrs and (attrs & hidden_bit):
            new_attrs = (attrs & ~hidden_bit) or normal_bit
            set_attrs(str(file_path), new_attrs)
    except Exception:
        pass


def atomic_copy_to_destination(src_file: Path, dest_dir: Path, dest_name: str | None = None) -> Path:
    """
    Two-stage Syncthing-safe cross-filesystem commit:
      1. Stream bytes to `<dest_dir>/~syncthing~<dest_name>.tmp` (natively ignored by
         Syncthing on all OSes without triggering Samba dotfile `Hidden` attributes).
      2. Verify destination byte count matches `src_file` and is > 0.
      3. Perform same-volume `os.replace()` -> `<dest_dir>/<dest_name>` for instant reveal.
    """
    if not src_file.is_file():
        raise RoutingError(f"Source file for routing does not exist: {src_file}")

    expected_size = src_file.stat().st_size
    if expected_size <= 0:
        raise RoutingError(f"Refusing to route 0-byte file: {src_file.name}")

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RoutingError(f"Cannot create/access destination directory `{dest_dir}`: {exc}") from exc

    final_name = dest_name or src_file.name
    final_path = dest_dir / final_name
    tmp_path = dest_dir / f"~syncthing~{final_name}.tmp"

    try:
        with src_file.open("rb") as fsrc, tmp_path.open("wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=COPY_BUFFER_BYTES)
            fdst.flush()
            try:
                os.fsync(fdst.fileno())
            except OSError:
                # Some SMB/GVFS FUSE implementations do not support explicit fsync
                pass

        # Best-effort metadata preservation (ignored on Linux GVFS SMB where chmod/utime may fail)
        try:
            shutil.copystat(src_file, tmp_path)
        except OSError:
            pass

        actual_size = tmp_path.stat().st_size
        if actual_size != expected_size or actual_size <= 0:
            raise RoutingError(
                f"Size mismatch after network copy for `{final_name}`: "
                f"expected {expected_size} B, got {actual_size} B."
            )

        os.replace(tmp_path, final_path)
        _clear_windows_hidden_attribute(final_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        if isinstance(exc, RoutingError):
            raise
        raise RoutingError(f"Atomic transfer failed for `{final_name}` -> `{dest_dir}`: {exc}") from exc

    return final_path


def atomic_copy_directory_to_destination(
    src_dir: Path,
    dest_dir: Path,
    dest_name: str | None = None,
) -> Path:
    """
    Atomically copy a structured ROM folder (e.g. Wii U Loadiine `{code, content, meta}`)
    to `dest_dir` via a `~syncthing~*.tmp` folder so Syncthing never syncs partial trees.
    """
    if not src_dir.is_dir():
        raise RoutingError(f"Source directory not found: {src_dir}")

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RoutingError(f"Cannot create/access destination directory `{dest_dir}`: {exc}") from exc

    final_name = dest_name or src_dir.name
    final_dir = dest_dir / final_name
    tmp_dir = dest_dir / f"~syncthing~{final_name}.tmp"
    backup_dir: Path | None = None

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(p for p in src_dir.rglob("*") if p.is_file()):
            rel = src_file.relative_to(src_dir)
            dst_file = tmp_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            expected_size = src_file.stat().st_size
            with src_file.open("rb") as fsrc, dst_file.open("wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, length=COPY_BUFFER_BYTES)
                fdst.flush()
            if dst_file.stat().st_size != expected_size:
                raise RoutingError(f"Size mismatch after network copy for `{rel}` inside `{final_name}`.")
            _clear_windows_hidden_attribute(dst_file)

        if final_dir.exists():
            backup_dir = dest_dir / f"~syncthing~old_{uuid.uuid4().hex[:8]}.tmp"
            os.replace(final_dir, backup_dir)
        os.replace(tmp_dir, final_dir)
        _clear_windows_hidden_attribute(final_dir)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists() and not final_dir.exists():
            try:
                os.replace(backup_dir, final_dir)
            except OSError:
                pass
        if isinstance(exc, RoutingError):
            raise
        raise RoutingError(f"Atomic directory transfer failed for `{final_name}` -> `{dest_dir}`: {exc}") from exc

    return final_dir


def update_m3u_playlist(
    system_dir: Path,
    multidisc_subdir: str,
    base_title: str,
) -> Path:
    """
    Idempotently generate/update `<system_dir>/<base_title>.m3u` using strictly
    POSIX forward-slash relative paths so Android emulators resolve discs cleanly.
    """
    candidates: list[tuple[int, str, str]] = []  # (disc_num, sort_name, posix_rel_path)

    # 1. Scan `_discs/` subfolder (primary home for multi-disc files)
    if multidisc_subdir:
        sub_path = system_dir / multidisc_subdir
        if sub_path.is_dir():
            for f in sub_path.iterdir():
                if not f.is_file() or _is_syncthing_temp(f.name):
                    continue
                if f.suffix.lower() not in DISC_IMAGE_EXTS_FOR_M3U:
                    continue
                parsed = parse_multidisc_info(f.stem)
                if parsed and parsed[0].lower() == base_title.lower():
                    rel_posix = str(PurePosixPath(multidisc_subdir) / f.name)
                    candidates.append((parsed[1], f.name.lower(), rel_posix))

    # 2. Also scan top-level `system_dir` in case an earlier disc was placed there manually
    if system_dir.is_dir():
        for f in system_dir.iterdir():
            if not f.is_file() or _is_syncthing_temp(f.name):
                continue
            if f.suffix.lower() not in DISC_IMAGE_EXTS_FOR_M3U:
                continue
            parsed = parse_multidisc_info(f.stem)
            if parsed and parsed[0].lower() == base_title.lower():
                candidates.append((parsed[1], f.name.lower(), f.name))

    candidates.sort(key=lambda item: (item[0], item[1]))
    playlist_lines = [item[2] for item in candidates]
    content = ("\n".join(playlist_lines) + "\n").encode("utf-8")

    m3u_name = f"{base_title}.m3u"
    m3u_final = system_dir / m3u_name
    m3u_tmp = system_dir / f"~syncthing~{m3u_name}.tmp"

    try:
        m3u_tmp.write_bytes(content)
        os.replace(m3u_tmp, m3u_final)
        _clear_windows_hidden_attribute(m3u_final)
    except OSError as exc:
        m3u_tmp.unlink(missing_ok=True)
        raise RoutingError(f"Failed writing `.m3u` playlist `{m3u_final}`: {exc}") from exc

    return m3u_final


def route_converted_artifact(
    artifact: ConversionArtifact,
    library_root: Path,
    config: PipelineConfig,
    dry_run: bool = False,
) -> tuple[list[Path], list[Path]]:
    """
    Route all output files of a `ConversionArtifact` into `library_root/<system_folder>`
    and generate/update `.m3u` playlists when applicable.

    Returns `(routed_file_paths, m3u_playlist_paths)`.
    """
    sys_cfg = config.systems[artifact.system_id]
    system_dir = library_root / sys_cfg.folder
    routed: list[Path] = []
    playlists: list[Path] = []

    for out_file in artifact.output_files:
        if out_file.is_dir() or (artifact.is_directory and out_file.suffix.lower() != ".dpt"):
            clean_dir_name = sanitize_rom_stem(out_file.name)
            dir_target = (
                system_dir / config.conversion.vita_addons_subdir
                if artifact.system_id == "psvita" and artifact.is_vita_addon and config.conversion.vita_addons_subdir
                else system_dir
            )
            if dry_run:
                routed.append(dir_target / clean_dir_name)
            else:
                routed.append(atomic_copy_directory_to_destination(out_file, dir_target, dest_name=clean_dir_name))
            continue

        clean_stem = (
            out_file.stem
            if sys_cfg.archive_policy == ArchivePolicy.PASSTHROUGH
            else sanitize_rom_stem(out_file.stem)
        )
        clean_name = f"{clean_stem}{out_file.suffix}"

        # 1a. Install-only 3DS `.cia` updates/DLC -> `3ds/_cia_install/`
        if artifact.system_id == "3ds" and artifact.is_installable_cia and config.conversion.cia_subdir:
            target_dir = system_dir / config.conversion.cia_subdir
            if dry_run:
                routed.append(target_dir / clean_name)
            else:
                routed.append(atomic_copy_to_destination(out_file, target_dir, dest_name=clean_name))
            continue

        # 1b. Standalone PS Vita Updates (`gp`), DLC (`ac`), and license sidecars (`work.bin`/`.zrif`/`zrif.txt`) -> `psvita/_updates_dlc/`
        is_vita_license_sidecar = artifact.system_id == "psvita" and (
            out_file.name.lower() == "work.bin"
            or out_file.name.lower().endswith("_work.bin")
            or out_file.suffix.lower() == ".zrif"
            or out_file.name.lower() == "zrif.txt"
            or out_file.name.lower().endswith(".zrif.txt")
        )
        if (
            artifact.system_id == "psvita"
            and (artifact.is_vita_addon or is_vita_license_sidecar)
            and config.conversion.vita_addons_subdir
        ):
            target_dir = system_dir / config.conversion.vita_addons_subdir
            if dry_run:
                routed.append(target_dir / clean_name)
            else:
                routed.append(atomic_copy_to_destination(out_file, target_dir, dest_name=clean_name))
            continue

        # 2. PlayStation Subchannel / LibCrypt sidecars -> `<system>/subchannels/<filename>`
        #    Ensures DuckStation automatically finds subchannel sector replacements while
        #    preventing frontends (Cocoon FE, Daijisho, EmulationStation) from indexing .sbi
        #    files as ghost duplicate game cards in the root library.
        is_sbi = out_file.suffix.lower() == ".sbi"
        if is_sbi and artifact.system_id == "psx":
            sub_dir = system_dir / "subchannels"
            if dry_run:
                routed.append(sub_dir / clean_name)
            else:
                routed.append(atomic_copy_to_destination(out_file, sub_dir, dest_name=clean_name))
            continue

        # 3. Multi-Disc Optical Games -> `<system>/_discs/` + `<system>/<Title>.m3u`
        #    (Skipped for PSX when `psx_multidisc_pbp` is active, since multi-disc PS1 sets
        #     merge into a single top-level `<Title>.pbp` for standalone DuckStation + Cocoon FE.)
        use_m3u = (
            config.conversion.generate_m3u
            and artifact.system_id in M3U_CAPABLE_SYSTEMS
            and (out_file.suffix.lower() in DISC_IMAGE_EXTS_FOR_M3U or is_sbi)
            and not (artifact.system_id == "psx" and config.conversion.psx_multidisc_pbp)
        )
        disc_info = parse_multidisc_info(clean_stem) if use_m3u else None

        if disc_info is not None:
            base_title, _ = disc_info
            subdir = config.conversion.multidisc_subdir
            disc_dest_dir = (system_dir / subdir) if subdir else system_dir
            if dry_run:
                routed.append(disc_dest_dir / clean_name)
                if not is_sbi:
                    playlists.append(system_dir / f"{base_title}.m3u")
            else:
                routed.append(atomic_copy_to_destination(out_file, disc_dest_dir, dest_name=clean_name))
                if not is_sbi:
                    playlists.append(update_m3u_playlist(system_dir, subdir, base_title))
            continue

        # 3. Standard single-file destination -> `<system>/<filename>`
        if dry_run:
            routed.append(system_dir / clean_name)
        else:
            routed.append(atomic_copy_to_destination(out_file, system_dir, dest_name=clean_name))

    return routed, playlists


def route_to_quarantine(
    file_path: Path,
    config: PipelineConfig,
    dry_run: bool = False,
) -> Path:
    """Safely copy an unidentified or encrypted ROM into `quarantine_dir` without collision."""
    q_dir = config.paths.quarantine_dir
    q_dir.mkdir(parents=True, exist_ok=True)
    target_name = _collision_safe_name(q_dir, file_path.name)
    if dry_run:
        return q_dir / target_name
    return atomic_copy_to_destination(file_path, q_dir, dest_name=target_name)


def _collision_safe_name(directory: Path, desired_name: str) -> str:
    """Return `desired_name` or `<stem>_1<ext>` if `desired_name` already exists in `directory`."""
    if not (directory / desired_name).exists():
        return desired_name
    p = Path(desired_name)
    stem, suffix = p.stem, p.suffix
    idx = 1
    while True:
        cand = f"{stem}_{idx}{suffix}"
        if not (directory / cand).exists():
            return cand
        idx += 1


def _finalize_source_item(
    source_path: Path,
    config: PipelineConfig,
    had_quarantine: bool,
) -> str:
    """Move source item to `processed_dir` or delete it per `config.general.post_process_action`."""
    # Safety guard: never delete/move files that already live inside `library_root` or `quarantine_dir`
    resolved_src = source_path.resolve()
    for protected in (config.paths.library_root, config.paths.quarantine_dir, config.paths.processed_dir):
        try:
            if resolved_src.is_relative_to(protected.resolve()):
                return "kept (source is inside protected library/processed/quarantine path)"
        except OSError:
            pass

    action = config.general.post_process_action
    # Safety override: never auto-delete an item that produced quarantined files
    if had_quarantine and action == PostProcessAction.DELETE:
        action = PostProcessAction.MOVE

    # If `source_path` is a loose `.cue`, `.gdi`, or `.pkg`, gather its companion files before moving it
    companion_tracks: list[Path] = []
    if source_path.is_file() and source_path.suffix.lower() == ".cue":
        companion_tracks = parse_cue_tracks(source_path)
    elif source_path.is_file() and source_path.suffix.lower() == ".gdi":
        companion_tracks = parse_gdi_tracks(source_path)
    elif source_path.is_file() and source_path.suffix.lower() == ".pkg":
        companion_tracks = list(_find_pkg_license_companions(source_path))

    # Gamarr writes `<dest>.gamarr.json` next to imported ROM files in `drop/<platform>/`
    gamarr_sidecar = source_path.parent / f"{source_path.name}.gamarr.json"
    if gamarr_sidecar.is_file():
        companion_tracks.append(gamarr_sidecar)

    if action == PostProcessAction.MOVE:
        proc_dir = config.paths.processed_dir
        proc_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _collision_safe_name(proc_dir, source_path.name)
        dest = proc_dir / safe_name
        shutil.move(str(source_path), str(dest))
        for trk in companion_tracks:
            if trk.exists():
                trk_safe = _collision_safe_name(proc_dir, trk.name)
                shutil.move(str(trk), str(proc_dir / trk_safe))
        return f"moved source -> {dest}"

    # PostProcessAction.DELETE
    if source_path.is_dir():
        shutil.rmtree(source_path)
    else:
        source_path.unlink(missing_ok=True)
    for trk in companion_tracks:
        trk.unlink(missing_ok=True)
    return "deleted source"


def _resolve_active_library_root(config: PipelineConfig, dry_run: bool) -> Path:
    """Resolve and verify NAS `library_root` (auto-mounting Fedora `smb://` if configured)."""
    if dry_run:
        return config.paths.library_root

    lib_root, smb_err = try_mount_smb_if_needed(config.paths, config.platform_key)
    if not lib_root.exists():
        detail = f" ({smb_err})" if smb_err else ""
        raise RoutingError(
            f"NAS `library_root` is unreachable: `{lib_root}`{detail}. "
            "Ensure your NAS share or library destination is mounted before processing."
        )
    return lib_root


def _can_keep_zip_passthrough(
    arc_path: Path,
    entries_count: int,
    first_ext: str,
    config: PipelineConfig,
) -> str | None:
    """Return `system_id` if archive is a single-ROM `.zip` configured with `ArchivePolicy.KEEP_ZIP`."""
    if arc_path.suffix.lower() != ".zip" or entries_count != 1:
        return None
    sys_id = UNAMBIGUOUS_EXTENSION_MAP.get(first_ext)
    if not sys_id or sys_id not in config.systems:
        return None
    if sys_id in ("3ds", "switch"):
        return None
    if config.systems[sys_id].archive_policy == ArchivePolicy.KEEP_ZIP:
        return sys_id
    return None


def find_nas_psx_multidisc_files(
    psx_dir: Path,
    multidisc_subdir: str,
    base_title: str,
) -> tuple[dict[int, Path], list[Path]]:
    """
    Scan `psx_dir` and `psx_dir/<multidisc_subdir>` for existing multi-disc PS1 artifacts
    belonging to `base_title`:
      - Returns `(donor_disc_by_number, superseded_nas_files)`.
      - `donor_disc_by_number` maps `disc_num -> Path` (`.chd`/`.cue`/`.iso`) so if Disc 1 was
        routed earlier and Disc 2 arrives later, both can be merged into `<base_title>.pbp`.
      - `superseded_nas_files` includes `<base_title>.m3u` and individual `(Disc N)` files so
        once `<base_title>.pbp` is verified on NAS, obsolete `.m3u`/`_discs/*.chd` are cleaned up.
    """
    donors: dict[int, Path] = {}
    superseded: list[Path] = []
    seen: set[Path] = set()
    target_lower = base_title.lower()

    def _record_superseded(p: Path) -> None:
        if p not in seen:
            seen.add(p)
            superseded.append(p)

    search_dirs: list[Path] = []
    if multidisc_subdir:
        sub_p = psx_dir / multidisc_subdir
        if sub_p.is_dir():
            search_dirs.append(sub_p)
    if psx_dir.is_dir():
        search_dirs.append(psx_dir)

    donor_priority = {".chd": 0, ".cue": 1, ".iso": 2, ".img": 3, ".bin": 4}

    for folder in search_dirs:
        try:
            entries = sorted(folder.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            continue

        for f in entries:
            if not f.is_file() or _is_syncthing_temp(f.name):
                continue
            ext = f.suffix.lower()
            clean_stem = sanitize_rom_stem(f.stem)

            if ext == ".m3u" and clean_stem.lower() == target_lower:
                _record_superseded(f)
                continue

            if ext not in donor_priority:
                continue

            parsed = parse_multidisc_info(clean_stem)
            if parsed is None or parsed[0].lower() != target_lower:
                continue

            disc_num = parsed[1]
            _record_superseded(f)
            if ext == ".cue":
                for trk in parse_cue_tracks(f):
                    _record_superseded(trk)

            existing = donors.get(disc_num)
            if existing is None or donor_priority[ext] < donor_priority.get(existing.suffix.lower(), 99):
                donors[disc_num] = f

    return donors, superseded


def _cleanup_superseded_psx_nas_files(
    psx_dir: Path,
    multidisc_subdir: str,
    routed_pbp_files: list[Path],
    superseded_candidates: list[Path],
) -> list[Path]:
    """Delete superseded `.m3u` and individual `(Disc N)` files once `.pbp` is verified on NAS."""
    if not routed_pbp_files or not all(p.is_file() and p.stat().st_size > 0 for p in routed_pbp_files):
        return []

    protected = {p.resolve() for p in routed_pbp_files}
    removed: list[Path] = []
    for old_file in superseded_candidates:
        try:
            if old_file.exists() and old_file.resolve() not in protected:
                old_file.unlink(missing_ok=True)
                removed.append(old_file)
        except OSError:
            pass

    if multidisc_subdir:
        sub_dir = psx_dir / multidisc_subdir
        try:
            if sub_dir.is_dir() and not any(sub_dir.iterdir()):
                sub_dir.rmdir()
        except OSError:
            pass

    return removed


def _process_identified_list(
    items: list[IdentificationResult],
    job_dir: Path,
    library_root: Path,
    config: PipelineConfig,
    dry_run: bool,
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """
    Convert and route a batch of identified items inside `job_dir`.

    Returns `(routed_files, m3u_playlists, quarantined_files, superseded_files)`.
    """
    routed_all: list[Path] = []
    m3u_all: list[Path] = []
    quarantined_all: list[Path] = []
    superseded_all: list[Path] = []
    consumed_indices: set[int] = set()

    # 1. Group multi-disc PS1 sets into single `<Base Title>.pbp` packages when configured
    if config.conversion.psx_multidisc_pbp and "psx" in config.systems:
        psx_dir = library_root / config.systems["psx"].folder
        psx_groups: dict[str, tuple[str, list[tuple[int, int, IdentificationResult]]]] = {}

        for idx, item in enumerate(items):
            if item.system_id != "psx" or item.is_encrypted_3ds:
                continue
            if item.primary_file.suffix.lower() in (".pbp", ".m3u"):
                continue
            clean_stem = sanitize_rom_stem(item.primary_file.stem, item.companion_files)
            parsed = parse_multidisc_info(clean_stem)
            if parsed is None:
                continue
            base_title, disc_num = parsed
            key = base_title.lower()
            if key not in psx_groups:
                psx_groups[key] = (base_title, [])
            psx_groups[key][1].append((idx, disc_num, item))

        for _key, (base_title, indexed_entries) in psx_groups.items():
            staged_disc_nums = {d_num for _, d_num, _ in indexed_entries}
            nas_donors, nas_superseded = find_nas_psx_multidisc_files(
                psx_dir,
                config.conversion.multidisc_subdir,
                base_title,
            )
            missing_donor_nums = sorted(d for d in nas_donors if d not in staged_disc_nums)
            total_discs = len(indexed_entries) + len(missing_donor_nums)

            if total_discs < 2:
                continue

            for idx, _, _ in indexed_entries:
                consumed_indices.add(idx)

            if dry_run:
                pred_pbp = job_dir / f"{base_title}.pbp"
                preview_art = ConversionArtifact(
                    system_id="psx",
                    output_files=(pred_pbp,),
                    converted=True,
                    tool_used="psxpackager",
                )
                r_files, m_files = route_converted_artifact(preview_art, library_root, config, dry_run=True)
                routed_all.extend(r_files)
                m3u_all.extend(m_files)
                superseded_all.extend(p for p in nas_superseded if p not in r_files)
                continue

            combined_disc_entries: list[tuple[int, IdentificationResult]] = [
                (d_num, it) for _, d_num, it in indexed_entries
            ]

            # Pull any earlier NAS donor discs (`.chd`/`.iso`/`.cue`) into local staging first
            if missing_donor_nums:
                donor_stage_dir = job_dir / f"_nas_psx_donor_{uuid.uuid4().hex[:6]}"
                donor_stage_dir.mkdir(parents=True, exist_ok=True)
                for d_num in missing_donor_nums:
                    nas_disc = nas_donors[d_num]
                    staged_disc = donor_stage_dir / nas_disc.name
                    with nas_disc.open("rb") as fsrc, staged_disc.open("wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst, length=COPY_BUFFER_BYTES)
                    if nas_disc.suffix.lower() == ".cue":
                        for trk in parse_cue_tracks(nas_disc):
                            staged_trk = donor_stage_dir / trk.name
                            with trk.open("rb") as fsrc, staged_trk.open("wb") as fdst:
                                shutil.copyfileobj(fsrc, fdst, length=COPY_BUFFER_BYTES)
                    donor_id = identify_single_file(
                        staged_disc,
                        chdman_bin=config.binaries.chdman,
                        project_root=config.paths.project_root,
                    )
                    combined_disc_entries.append((d_num, donor_id))

            pbp_artifact = merge_psx_multidisc_pbp(
                combined_disc_entries,
                base_title,
                job_dir,
                config,
            )
            r_files, m_files = route_converted_artifact(pbp_artifact, library_root, config, dry_run=False)
            routed_all.extend(r_files)
            m3u_all.extend(m_files)
            superseded_all.extend(
                _cleanup_superseded_psx_nas_files(
                    psx_dir,
                    config.conversion.multidisc_subdir,
                    r_files,
                    nas_superseded,
                )
            )

    # 2. Process all remaining individual items
    for idx, item in enumerate(items):
        if idx in consumed_indices:
            continue

        if (
            not item.system_id
            or item.system_id not in config.systems
            or (
                item.is_encrypted_3ds
                and not (item.is_zerokey_3ds or can_decrypt_3ds(item.primary_file, config))
            )
            or (item.is_encrypted_wiiu and config.binaries.cdecrypt is None)
        ):
            q_path = route_to_quarantine(item.primary_file, config, dry_run=dry_run)
            quarantined_all.append(q_path)
            continue

        if dry_run:
            # Preview conversion output filename without invoking external conversion binaries
            preview_art = _build_dry_run_artifact(item, job_dir, config)
            r_files, m_files = route_converted_artifact(preview_art, library_root, config, dry_run=True)
            routed_all.extend(r_files)
            m3u_all.extend(m_files)
            continue

        artifact = convert_identified_item(item, job_dir, config)
        r_files, m_files = route_converted_artifact(artifact, library_root, config, dry_run=False)
        routed_all.extend(r_files)
        m3u_all.extend(m_files)

    return routed_all, m3u_all, quarantined_all, superseded_all


def _normalize_wiiu_root_titles(
    items: list[IdentificationResult],
    source_path: Path,
) -> list[IdentificationResult]:
    """Populate a fallback clean title from `source_path` for root-extracted Wii U / PS Vita folders."""
    fallback = sanitize_rom_stem(source_path.stem if source_path.is_file() else source_path.name)
    out: list[IdentificationResult] = []
    for it in items:
        if it.system_id == "wiiu" and not it.wiiu_clean_title and fallback:
            out.append(dataclass_replace(it, wiiu_clean_title=fallback))
        elif it.system_id == "psvita" and not it.vita_clean_title and fallback:
            out.append(dataclass_replace(it, vita_clean_title=fallback))
        else:
            out.append(it)
    return out


def _should_quarantine_source_archive_whole(
    items: list[IdentificationResult],
    config: PipelineConfig,
) -> bool:
    """
    Return True when an extracted archive should be quarantined intact as a single file
    instead of exploding extracted temporary entries flat into `quarantine/`.
    """
    if not items:
        return True
    if all(not it.is_identified for it in items):
        return True
    if (
        len(items) == 1
        and items[0].is_encrypted_wiiu
        and config.binaries.cdecrypt is None
    ):
        return True
    return False


def _build_dry_run_artifact(
    item: IdentificationResult,
    job_dir: Path,
    config: PipelineConfig,
) -> ConversionArtifact:
    """Predict output filenames for `--dry-run` without running heavy CPU conversions."""
    return build_dry_run_artifact(item, job_dir, config)


def _stage_and_identify_archive(
    arc_info: ArchiveInspection,
    source_path: Path,
    config: PipelineConfig,
    *,
    dry_run: bool,
    job_dir: Path,
    sub_id: str = "",
) -> tuple[Path, list[IdentificationResult]]:
    """
    Stage `arc_info` inside `job_dir` (header-preview with full fallback in `dry_run`,
    or full extraction in live mode) and return `(staged_dir, normalized_items)`.
    """
    job_rel = f"{job_dir.name}/{sub_id}" if sub_id else job_dir.name
    if dry_run:
        preview_target = job_dir / (f"_preview_{sub_id}" if sub_id else "_preview")
        p_dir, logical_sizes = extract_header_preview_to_staging(
            arc_info,
            config,
            target_dir=preview_target,
        )
        try:
            items = identify_directory(
                p_dir,
                logical_sizes=logical_sizes,
                chdman_bin=config.binaries.chdman,
                project_root=config.paths.project_root,
            )
            active_dir = p_dir
        except TruncatedPreviewError:
            active_dir = extract_archive_to_staging(arc_info, config, job_id=job_rel)
            items = identify_directory(
                active_dir,
                chdman_bin=config.binaries.chdman,
                project_root=config.paths.project_root,
            )
    else:
        active_dir = extract_archive_to_staging(arc_info, config, job_id=job_rel)
        items = identify_directory(
            active_dir,
            chdman_bin=config.binaries.chdman,
            project_root=config.paths.project_root,
        )

    return active_dir, _normalize_wiiu_root_titles(items, source_path)


def _finalize_sources(
    sources: list[Path],
    config: PipelineConfig,
    *,
    dry_run: bool,
    keep_source: bool,
    has_routed: bool,
    has_quarantined: bool,
    deferred_notes: list[str] | None = None,
) -> str:
    """Finalize one or more source paths (`move` to `processed/` or `delete`) after routing."""
    if dry_run:
        return "dry-run (source untouched)"
    if keep_source:
        return "kept source (--keep-source)"
    if deferred_notes and not has_routed and not has_quarantined:
        return "; ".join(deferred_notes)
    if has_routed or has_quarantined:
        notes = [
            _finalize_source_item(s, config, had_quarantine=has_quarantined)
            for s in sources
        ]
        return notes[0] if len(notes) == 1 else f"finalized {len(sources)} sources ({notes[0]}; ...)"
    return "skipped (no actionable ROMs found)"


def _build_routing_outcome(
    sources: list[Path],
    config: PipelineConfig,
    *,
    routed_files: list[Path],
    m3u_files: list[Path],
    quarantined_files: list[Path],
    superseded_files: list[Path],
    dry_run: bool,
    keep_source: bool,
    deferred_notes: list[str] | None = None,
) -> RoutingOutcome:
    """Deduplicate output lists, finalize source(s), and build a `RoutingOutcome`."""
    unique_m3u = tuple(dict.fromkeys(m3u_files))
    unique_sup = tuple(dict.fromkeys(superseded_files))
    post_note = _finalize_sources(
        sources,
        config,
        dry_run=dry_run,
        keep_source=keep_source,
        has_routed=bool(routed_files),
        has_quarantined=bool(quarantined_files),
        deferred_notes=deferred_notes,
    )
    return RoutingOutcome(
        source_path=sources[0],
        routed_files=tuple(routed_files),
        m3u_playlists=unique_m3u,
        quarantined_files=tuple(quarantined_files),
        post_action_taken=post_note,
        dry_run=dry_run,
        additional_sources=tuple(sources[1:]),
        superseded_files=unique_sup,
    )


def ingest_switch_bundle(
    source_paths: list[Path],
    config: PipelineConfig,
    dry_run: bool = False,
    keep_source: bool = False,
) -> RoutingOutcome:
    """
    Unified Switch family ingestion supporting:
      - Separate Base + Update + DLC archives/files grouped across `watch_dir` or CLI arguments
      - Folders mixing loose `.nsp`/`.xci` files with nested `.rar`/`.zip`/`.7z` archives
      - Automatic NAS Base donor upgrades when an Update/DLC arrives for an already-routed game
      - Safe deferral when an Update/DLC arrives without its Base game (never builds a DLC-only XCI)
    """
    ensure_local_directories(config)
    if not source_paths:
        raise RoutingError("No source paths provided for Switch bundle ingestion.")

    srcs = [p.expanduser().resolve() for p in source_paths]
    for s in srcs:
        if not s.exists():
            raise RoutingError(f"Source path does not exist: {s}")

    if "switch" not in config.systems:
        raise RoutingError("`systems.switch` is not configured in config.toml.")

    library_root = _resolve_active_library_root(config, dry_run=dry_run)
    switch_dest_dir = library_root / config.systems["switch"].folder

    probed_infos = [probe_switch_source(s, config) for s in srcs]
    all_fams: list[str] = []
    combined_roles: set[SwitchContentRole] = set()
    for info in probed_infos:
        if info is not None:
            for f in info.family_ids:
                if f not in all_fams:
                    all_fams.append(f)
            combined_roles.update(info.roles)

    from src.switch_bundler import _normalize_switch_title_key

    primary_raw_name = srcs[0].name if srcs[0].is_dir() else srcs[0].stem

    # Fast pre-extraction guard: if sources contain ONLY Updates/DLCs and NAS has no Base donor, defer!
    if (
        all(info is not None for info in probed_infos)
        and len(all_fams) == 1
        and SwitchContentRole.BASE not in combined_roles
    ):
        fam_only = all_fams[0]
        title_hint = sanitize_rom_stem(primary_raw_name)
        nas_donor, _ = find_nas_switch_family_files(
            switch_dest_dir, fam_only, config, game_title_hint=title_hint
        )
        if nas_donor is None:
            return RoutingOutcome(
                source_path=srcs[0],
                additional_sources=tuple(srcs[1:]),
                post_action_taken=(
                    f"deferred (Switch Update/DLC [{fam_only}] waiting for Base game "
                    f"in `{config.paths.watch_dir.name}/` or NAS `{config.systems['switch'].folder}/`)"
                ),
                dry_run=dry_run,
            )

    # Dry-run simulation without extracting multi-GB Switch archives or copying NAS donors
    if dry_run:
        total_pkgs = sum(i.package_count for i in probed_infos if i is not None)
        fam_id = all_fams[0] if len(all_fams) == 1 else None
        nas_donor: Path | None = None
        existing_nas: list[Path] = []
        if fam_id is not None:
            nas_donor, existing_nas = find_nas_switch_family_files(
                switch_dest_dir, fam_id, config, game_title_hint=sanitize_rom_stem(primary_raw_name)
            )
            if SwitchContentRole.BASE not in combined_roles and nas_donor is not None:
                total_pkgs += 1

        if total_pkgs >= 2:
            donor_or_primary = (
                nas_donor
                if (SwitchContentRole.BASE not in combined_roles and nas_donor is not None)
                else srcs[0]
            )
            stem_for_pred = (
                donor_or_primary.name if donor_or_primary.is_dir() else donor_or_primary.stem
            )
            predicted = (
                switch_dest_dir
                / f"{stem_for_pred} [Merged].{config.conversion.switch_merge_format}"
            )
            superseded = tuple(p for p in existing_nas if p != predicted)
        else:
            ext = srcs[0].suffix.lower()
            if ext in COMPRESSED_SWITCH_EXTENSIONS and config.conversion.decompress_nsz:
                out_ext = ".nsp" if ext == ".nsz" else ".xci"
                predicted = switch_dest_dir / f"{srcs[0].stem}{out_ext}"
            elif ext in SWITCH_EXTENSIONS:
                predicted = switch_dest_dir / srcs[0].name
            else:
                predicted = switch_dest_dir / f"{srcs[0].stem}.nsp"
            superseded = ()

        return RoutingOutcome(
            source_path=srcs[0],
            routed_files=(predicted,),
            superseded_files=superseded,
            additional_sources=tuple(srcs[1:]),
            post_action_taken="dry-run (source untouched)",
            dry_run=True,
        )

    routed_files: list[Path] = []
    m3u_files: list[Path] = []
    quarantined_files: list[Path] = []
    superseded_files: list[Path] = []
    deferred_notes: list[str] = []

    with StagingJobDir(config, create=True) as job_dir:
        # 1. Inspect all archives in the bundle upfront and verify combined staging space
        archives_to_extract: list[tuple[str, Path]] = []
        loose_candidates: list[Path] = []

        for idx, s in enumerate(srcs):
            if s.is_file() and is_archive(s):
                archives_to_extract.append((f"src_{idx}", s))
            elif s.is_dir():
                for child in sorted(p for p in s.rglob("*") if p.is_file()):
                    if is_archive(child):
                        archives_to_extract.append((f"src_{idx}_arc_{len(archives_to_extract)}", child))
                    else:
                        loose_candidates.append(child)
            elif s.is_file():
                loose_candidates.append(s)

        inspected_arcs = [(sub_label, inspect_archive(arc_p, config)) for sub_label, arc_p in archives_to_extract]
        combined_uncompressed = sum(info.total_uncompressed_bytes for _, info in inspected_arcs)
        if combined_uncompressed > 0:
            check_staging_space(combined_uncompressed, config)

        # 2. Extract archives into isolated subdirs of `job_dir` & classify files
        gathered_switch_files: list[Path] = []
        unexpected_other_files: list[Path] = []

        def _collect_candidate_file(f_path: Path) -> None:
            if f_path.name.lower() in IGNORED_FILENAMES:
                return
            ext = f_path.suffix.lower()
            if ext in IGNORED_SIDECAR_EXTENSIONS:
                return
            if ext in SWITCH_EXTENSIONS:
                gathered_switch_files.append(f_path)
            else:
                unexpected_other_files.append(f_path)

        for f_loose in loose_candidates:
            _collect_candidate_file(f_loose)

        for sub_label, arc_info in inspected_arcs:
            extracted_sub = extract_archive_to_staging(
                arc_info,
                config,
                job_id=f"{job_dir.name}/{sub_label}",
            )
            for extr_file in sorted(p for p in extracted_sub.rglob("*") if p.is_file()):
                _collect_candidate_file(extr_file)

        # 3. Group Switch packages by Horizon OS `family_id`
        family_map: dict[str, list[Path]] = {}
        untagged_switch: list[Path] = []
        for sw_file in gathered_switch_files:
            tids = extract_switch_title_ids_from_file(sw_file)
            if tids:
                fam_id, _ = classify_switch_title_id(tids[0])
                family_map.setdefault(fam_id, []).append(sw_file)
            else:
                untagged_switch.append(sw_file)

        if len(family_map) == 1 and untagged_switch:
            sole_fam = next(iter(family_map))
            family_map[sole_fam].extend(untagged_switch)
            untagged_switch.clear()
        elif len(family_map) > 1 and untagged_switch:
            remaining_untagged: list[Path] = []
            for u_pkg in untagged_switch:
                u_key = _normalize_switch_title_key(u_pkg.stem)
                matched_fam: str | None = None
                if u_key:
                    for cand_fam, fam_pkgs in family_map.items():
                        if any(_normalize_switch_title_key(fp.stem) == u_key for fp in fam_pkgs):
                            matched_fam = cand_fam
                            break
                if matched_fam is not None:
                    family_map[matched_fam].append(u_pkg)
                else:
                    remaining_untagged.append(u_pkg)
            untagged_switch = remaining_untagged

        # 4. Convert & atomically route each Switch family
        for fam_id, pkg_list in family_map.items():
            has_base_pkg = False
            for pkg in pkg_list:
                if has_explicit_addon_only_tag(pkg.name):
                    continue
                tids = extract_switch_title_ids_from_file(pkg)
                if not tids:
                    # Untagged Switch package (e.g. No-Intro `.xci` without TitleID in filename)
                    has_base_pkg = True
                    break
                for t in tids:
                    _, role = classify_switch_title_id(t)
                    if role == SwitchContentRole.BASE:
                        has_base_pkg = True
                        break

            nas_donor, existing_nas_files = find_nas_switch_family_files(
                switch_dest_dir,
                fam_id,
                config,
                game_title_hint=sanitize_rom_stem(primary_raw_name),
            )

            if not has_base_pkg:
                if nas_donor is None:
                    deferred_notes.append(
                        f"deferred [{fam_id}] (waiting for Base game in "
                        f"`{config.paths.watch_dir.name}/` or NAS `{config.systems['switch'].folder}/`)"
                    )
                    continue

                # Copy NAS Base/Merged donor into local staging so squirrel merges at NVMe speed
                donor_dir = job_dir / f"nas_donor_{fam_id}"
                donor_dir.mkdir(parents=True, exist_ok=True)
                staged_donor = donor_dir / nas_donor.name
                with nas_donor.open("rb") as fsrc, staged_donor.open("wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, length=COPY_BUFFER_BYTES)
                pkg_list = [staged_donor, *pkg_list]

            ordered_pkgs = sorted(pkg_list, key=switch_package_sort_key)
            is_multi = len(ordered_pkgs) >= 2
            has_comp = any(p.suffix.lower() in COMPRESSED_SWITCH_EXTENSIONS for p in ordered_pkgs)
            conv_sub = job_dir / f"conv_{fam_id}"
            conv_sub.mkdir(parents=True, exist_ok=True)

            id_item = IdentificationResult(
                system_id="switch",
                method=DetectionMethod.UNAMBIGUOUS_EXTENSION,
                confidence="high",
                reason=f"Switch family [{fam_id}] ({len(ordered_pkgs)} package(s))",
                primary_file=ordered_pkgs[0],
                companion_files=tuple(ordered_pkgs[1:]),
                is_compressed_switch=has_comp,
                is_multifile_switch=is_multi,
            )
            artifact = convert_identified_item(id_item, conv_sub, config)
            r_f, m_f = route_converted_artifact(artifact, library_root, config, dry_run=False)
            routed_files.extend(r_f)
            m3u_files.extend(m_f)

            # Once the newly merged `.xci` is verified on NAS, remove superseded split/older family files
            # SAFETY GUARD: Never delete an existing Base or Merged game on NAS if the newly routed
            # package is only an add-on (0G, e.g. `(1U+2D)` or `(5D)`) or lacks a Base game!
            new_is_addon_only = any(has_explicit_addon_only_tag(p.name) for p in r_f)
            if is_multi and not new_is_addon_only and r_f and all(p.is_file() and p.stat().st_size > 0 for p in r_f):
                routed_resolved = {p.resolve() for p in r_f}
                for old_nas in existing_nas_files:
                    try:
                        if old_nas.exists() and old_nas.resolve() not in routed_resolved:
                            old_nas.unlink(missing_ok=True)
                            superseded_files.append(old_nas)
                    except OSError:
                        pass

        # 5. Handle untagged standalone Switch ROMs (e.g. gamecard `.xci` without TitleID in filename)
        if untagged_switch:
            ordered_untagged = sorted(untagged_switch, key=switch_package_sort_key)
            is_multi_u = len(ordered_untagged) >= 2
            has_comp_u = any(p.suffix.lower() in COMPRESSED_SWITCH_EXTENSIONS for p in ordered_untagged)
            conv_u = job_dir / "conv_untagged"
            conv_u.mkdir(parents=True, exist_ok=True)
            id_u = IdentificationResult(
                system_id="switch",
                method=DetectionMethod.UNAMBIGUOUS_EXTENSION,
                confidence="high",
                reason="Untagged Switch package(s)",
                primary_file=ordered_untagged[0],
                companion_files=tuple(ordered_untagged[1:]),
                is_compressed_switch=has_comp_u,
                is_multifile_switch=is_multi_u,
            )
            art_u = convert_identified_item(id_u, conv_u, config)
            r_u, m_u = route_converted_artifact(art_u, library_root, config, dry_run=False)
            routed_files.extend(r_u)
            m3u_files.extend(m_u)

        # 6. Route/quarantine any unexpected non-Switch files that were bundled alongside
        if unexpected_other_files:
            other_items = [
                identify_single_file(
                    p,
                    chdman_bin=config.binaries.chdman,
                    project_root=config.paths.project_root,
                )
                for p in unexpected_other_files
            ]
            r_o, m_o, q_o, s_o = _process_identified_list(
                other_items,
                job_dir / "conv_other",
                library_root,
                config,
                dry_run=False,
            )
            routed_files.extend(r_o)
            m3u_files.extend(m_o)
            quarantined_files.extend(q_o)
            superseded_files.extend(s_o)

    return _build_routing_outcome(
        srcs,
        config,
        routed_files=routed_files,
        m3u_files=m3u_files,
        quarantined_files=quarantined_files,
        superseded_files=superseded_files,
        dry_run=False,
        keep_source=keep_source,
        deferred_notes=deferred_notes,
    )


def probe_multidisc_source_title(path: Path) -> tuple[str, int] | None:
    """
    If `path` carries a `(Disc N)` / `CD N` marker in its stem, return
    `(clean_base_title, disc_number)` so sibling archives/files in `watch_dir`
    can be bundled into a single multi-disc `.pbp` or `.m3u` pass.
    """
    raw_stem = path.name if path.is_dir() else path.stem
    return parse_multidisc_info(sanitize_rom_stem(raw_stem))


def group_ingestion_batches(
    candidates: list[Path],
    config: PipelineConfig,
) -> list[tuple[str, list[Path]]]:
    """
    Partition top-level candidates into ordered `(batch_kind, paths)` tuples:
      - `"switch"`    -> Multi-item Switch TitleID family bundle (`ingest_switch_bundle`)
      - `"multidisc"` -> 2+ sibling `(Disc 1..N)` archives/files sharing a title (`ingest_multidisc_bundle`)
      - `"single"`    -> Standalone archive, folder, or ROM (`ingest_target`)
    """
    switch_grouped = group_candidates_with_switch_families(candidates, config)
    multidisc_buckets: dict[str, list[tuple[int, Path]]] = {}
    ordered_slots: list[tuple[str, object]] = []

    for group in switch_grouped:
        if len(group) > 1:
            ordered_slots.append(("switch", group))
            continue

        item = group[0]
        if "switch" in config.systems and probe_switch_source(item, config) is not None:
            ordered_slots.append(("single", [item]))
            continue

        md_info = probe_multidisc_source_title(item)
        if md_info is not None:
            base_title, disc_num = md_info
            key = base_title.lower()
            if key not in multidisc_buckets:
                multidisc_buckets[key] = []
                ordered_slots.append(("multidisc_slot", key))
            multidisc_buckets[key].append((disc_num, item))
        else:
            ordered_slots.append(("single", [item]))

    final_batches: list[tuple[str, list[Path]]] = []
    for kind, payload in ordered_slots:
        if kind == "multidisc_slot":
            entries = sorted(
                multidisc_buckets[str(payload)],
                key=lambda pair: (pair[0], pair[1].name.lower()),
            )
            paths = [p for _, p in entries]
            if len(paths) >= 2:
                final_batches.append(("multidisc", paths))
            else:
                final_batches.append(("single", paths))
        else:
            final_batches.append((kind, list(payload)))  # type: ignore[arg-type]

    return final_batches


def ingest_multidisc_bundle(
    source_paths: list[Path],
    config: PipelineConfig,
    dry_run: bool = False,
    keep_source: bool = False,
) -> RoutingOutcome:
    """
    Unified ingestion for 2+ sibling `(Disc 1..N)` archives/files/folders sharing a game title
    so PS1 multi-disc sets merge directly into a single `<Title>.pbp` in one staging pass.
    """
    ensure_local_directories(config)
    if not source_paths:
        raise RoutingError("No source paths provided for multi-disc bundle ingestion.")
    if len(source_paths) == 1:
        return ingest_target(source_paths[0], config, dry_run=dry_run, keep_source=keep_source)

    srcs = [p.expanduser().resolve() for p in source_paths]
    for s in srcs:
        if not s.exists():
            raise RoutingError(f"Source path does not exist: {s}")

    library_root = _resolve_active_library_root(config, dry_run=dry_run)

    routed_files: list[Path] = []
    m3u_files: list[Path] = []
    quarantined_files: list[Path] = []
    superseded_files: list[Path] = []

    with StagingJobDir(config, create=True) as job_dir:
        archives = [(idx, s) for idx, s in enumerate(srcs) if s.is_file() and is_archive(s)]
        inspected = [(idx, inspect_archive(s, config)) for idx, s in archives]
        arc_map = {idx: info for idx, info in inspected}

        if not dry_run:
            total_uncomp = sum(info.total_uncompressed_bytes for _, info in inspected if not info.is_arcade)
            if total_uncomp > 0:
                check_staging_space(total_uncomp, config)

        combined_items: list[IdentificationResult] = []
        for idx, s in enumerate(srcs):
            if idx in arc_map:
                arc_info = arc_map[idx]
                if arc_info.is_arcade and arc_info.arcade_system_id in config.systems:
                    art = ConversionArtifact(
                        system_id=arc_info.arcade_system_id,
                        output_files=(s,),
                        converted=False,
                        summary=arc_info.reason,
                    )
                    r_f, m_f = route_converted_artifact(art, library_root, config, dry_run=dry_run)
                    routed_files.extend(r_f)
                    m3u_files.extend(m_f)
                else:
                    _, sub_items = _stage_and_identify_archive(
                        arc_info,
                        s,
                        config,
                        dry_run=dry_run,
                        job_dir=job_dir,
                        sub_id=f"src_{idx}",
                    )
                    if _should_quarantine_source_archive_whole(sub_items, config):
                        quarantined_files.append(route_to_quarantine(s, config, dry_run=dry_run))
                    else:
                        combined_items.extend(sub_items)
            elif s.is_dir():
                combined_items.extend(
                    identify_directory(
                        s,
                        chdman_bin=config.binaries.chdman,
                        project_root=config.paths.project_root,
                    )
                )
            else:
                combined_items.append(
                    identify_single_file(
                        s,
                        chdman_bin=config.binaries.chdman,
                        project_root=config.paths.project_root,
                    )
                )

        if combined_items:
            r_f, m_f, q_f, s_f = _process_identified_list(
                combined_items,
                job_dir,
                library_root,
                config,
                dry_run=dry_run,
            )
            routed_files.extend(r_f)
            m3u_files.extend(m_f)
            quarantined_files.extend(q_f)
            superseded_files.extend(s_f)

    return _build_routing_outcome(
        srcs,
        config,
        routed_files=routed_files,
        m3u_files=m3u_files,
        quarantined_files=quarantined_files,
        superseded_files=superseded_files,
        dry_run=dry_run,
        keep_source=keep_source,
    )


def _ingest_archive_source(
    src: Path,
    job_dir: Path,
    library_root: Path,
    config: PipelineConfig,
    dry_run: bool,
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Inspect, extract (if non-arcade/non-keep_zip), convert, and route a single archive source."""
    arc_info = inspect_archive(src, config)

    # 1a. Arcade Trap Prevention: DO NOT EXTRACT! Route raw archive directly.
    if arc_info.is_arcade and arc_info.arcade_system_id in config.systems:
        art = ConversionArtifact(
            system_id=arc_info.arcade_system_id,
            output_files=(src,),
            converted=False,
            summary=arc_info.reason,
        )
        r_f, m_f = route_converted_artifact(art, library_root, config, dry_run=dry_run)
        return r_f, m_f, [], []

    # 1b. Optional `keep_zip` cartridge policy
    if (
        len(arc_info.file_entries) == 1
        and (
            keep_sys := _can_keep_zip_passthrough(
                src, 1, arc_info.file_entries[0].suffix, config
            )
        )
    ):
        art = ConversionArtifact(
            system_id=keep_sys,
            output_files=(src,),
            converted=False,
        )
        r_f, m_f = route_converted_artifact(art, library_root, config, dry_run=dry_run)
        return r_f, m_f, [], []

    # 1c. Console / Disc Archive -> Stage & identify inside `job_dir`
    active_dir, items = _stage_and_identify_archive(
        arc_info,
        src,
        config,
        dry_run=dry_run,
        job_dir=job_dir,
    )
    if _should_quarantine_source_archive_whole(items, config):
        return [], [], [route_to_quarantine(src, config, dry_run=dry_run)], []

    conv_dir = job_dir if dry_run else active_dir
    return _process_identified_list(items, conv_dir, library_root, config, dry_run=dry_run)


def _ingest_directory_source(
    src: Path,
    job_dir: Path,
    library_root: Path,
    config: PipelineConfig,
    dry_run: bool,
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Identify, convert, and route a dropped directory (including any nested archives)."""
    routed_files: list[Path] = []
    m3u_files: list[Path] = []
    quarantined_files: list[Path] = []
    superseded_files: list[Path] = []

    job_dir.mkdir(parents=True, exist_ok=True)
    loose_items = _normalize_wiiu_root_titles(
        identify_directory(
            src,
            chdman_bin=config.binaries.chdman,
            project_root=config.paths.project_root,
        ),
        src,
    )
    if loose_items:
        r_f, m_f, q_f, s_f = _process_identified_list(
            loose_items, job_dir, library_root, config, dry_run=dry_run
        )
        routed_files.extend(r_f)
        m3u_files.extend(m_f)
        quarantined_files.extend(q_f)
        superseded_files.extend(s_f)

    # Also process any archives nested inside the dropped folder (keep_source=True until parent finishes)
    nested_archives = sorted(p for p in src.rglob("*") if p.is_file() and is_archive(p))
    for b_kind, b_paths in group_ingestion_batches(nested_archives, config):
        sub_outcome = execute_ingestion_batch(
            b_kind,
            b_paths,
            config,
            dry_run=dry_run,
            keep_source=True,
        )
        routed_files.extend(sub_outcome.routed_files)
        m3u_files.extend(sub_outcome.m3u_playlists)
        quarantined_files.extend(sub_outcome.quarantined_files)
        superseded_files.extend(sub_outcome.superseded_files)

    return routed_files, m3u_files, quarantined_files, superseded_files


def ingest_target(
    source_path: Path,
    config: PipelineConfig,
    dry_run: bool = False,
    keep_source: bool = False,
) -> RoutingOutcome:
    """
    End-to-end atomic ingestion pipeline for a single archive, ROM/disc file, or folder:
      1. Inspect & extract (if non-arcade archive) into `.staging/<job_uuid>/`
      2. Identify target console(s) via extension & binary magic headers
      3. Convert to optimal format (`.chd`, multi-disc PS1 `.pbp`, `.rvz`, merged Switch `.xci`, `.cci`)
      4. Commit atomically to `library_root` (`~syncthing~*.tmp` -> `os.replace`) + `.m3u`
      5. Clean up `.staging/<job_uuid>/` and move/delete `source_path`
    """
    ensure_local_directories(config)
    src = source_path.expanduser().resolve()
    if not src.exists():
        raise RoutingError(f"Source path does not exist: {src}")

    # Delegate Switch packages, Switch archives, and Switch folders to the family-aware bundler
    if "switch" in config.systems and probe_switch_source(src, config) is not None:
        return ingest_switch_bundle([src], config, dry_run=dry_run, keep_source=keep_source)

    library_root = _resolve_active_library_root(config, dry_run=dry_run)

    with StagingJobDir(config) as job_dir:
        if src.is_file() and is_archive(src):
            routed_files, m3u_files, quarantined_files, superseded_files = _ingest_archive_source(
                src, job_dir, library_root, config, dry_run=dry_run
            )
        elif src.is_dir():
            routed_files, m3u_files, quarantined_files, superseded_files = _ingest_directory_source(
                src, job_dir, library_root, config, dry_run=dry_run
            )
        else:
            job_dir.mkdir(parents=True, exist_ok=True)
            single_item = identify_single_file(
                src,
                chdman_bin=config.binaries.chdman,
                project_root=config.paths.project_root,
            )
            routed_files, m3u_files, quarantined_files, superseded_files = _process_identified_list(
                [single_item], job_dir, library_root, config, dry_run=dry_run
            )

    return _build_routing_outcome(
        [src],
        config,
        routed_files=routed_files,
        m3u_files=m3u_files,
        quarantined_files=quarantined_files,
        superseded_files=superseded_files,
        dry_run=dry_run,
        keep_source=keep_source,
    )


def execute_ingestion_batch(
    batch_kind: str,
    batch: list[Path],
    config: PipelineConfig,
    *,
    dry_run: bool = False,
    keep_source: bool = False,
) -> RoutingOutcome:
    """Execute a `(batch_kind, batch)` tuple produced by `group_ingestion_batches`."""
    if batch_kind == "switch" and len(batch) > 1:
        return ingest_switch_bundle(batch, config, dry_run=dry_run, keep_source=keep_source)
    if batch_kind == "multidisc" and len(batch) > 1:
        return ingest_multidisc_bundle(batch, config, dry_run=dry_run, keep_source=keep_source)
    return ingest_target(batch[0], config, dry_run=dry_run, keep_source=keep_source)
