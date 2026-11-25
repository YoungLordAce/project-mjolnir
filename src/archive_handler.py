"""Archive inspection ('Peek Before Extracting'), Arcade trap prevention, and staging extraction."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from src.arcade_db import classify_arcade_archive
from src.config_loader import check_seven_zip_supports_rar
from src.constants import SUPPORTED_ARCHIVE_EXTENSIONS
from src.models import ArchiveEntry, ArchiveInspection, PipelineConfig
from src.runner import run_capture, stream_stdout_slice

_TRACK_NUM_RE = re.compile(r"\btrack\s*0*(\d+)\b", re.IGNORECASE)
_SECONDARY_TRACK_EXTS: frozenset[str] = frozenset({".bin", ".raw", ".wav", ".ape", ".ogg"})


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be listed, validated, or extracted."""


class InsufficientStagingSpaceError(ArchiveError):
    """Raised when local staging disk lacks space to safely extract and convert an archive."""


_STAGING_LOCK_GUARD = threading.Lock()
_ACTIVE_STAGING_LOCKS: set[Path] = set()


class StagingLock:
    """Cross-platform non-blocking advisory lock protecting active `.staging/` jobs from stale-purge."""

    def __init__(self, lock_path: Path, fd: int) -> None:
        self.path = lock_path.resolve()
        self._fd: int | None = fd
        with _STAGING_LOCK_GUARD:
            _ACTIVE_STAGING_LOCKS.add(self.path)

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        with _STAGING_LOCK_GUARD:
            _ACTIVE_STAGING_LOCKS.discard(self.path)
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def acquire_staging_lock(lock_path: Path) -> StagingLock | None:
    """Acquire an exclusive non-blocking lock on `lock_path`; return `None` if unavailable."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
        return None

    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"1")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None

    return StagingLock(lock_path, fd)


def is_staging_lock_held(lock_path: Path) -> bool:
    """Return True if `lock_path` is actively held by this process or another live OS process."""
    try:
        resolved = lock_path.resolve()
    except OSError:
        return False

    with _STAGING_LOCK_GUARD:
        if resolved in _ACTIVE_STAGING_LOCKS:
            return True

    if not lock_path.is_file():
        return False

    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_RDWR)
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class StagingJobDir:
    """Context manager that allocates an isolated `.staging/<prefix><uuid>` directory and purges it on exit."""

    def __init__(
        self,
        config: PipelineConfig,
        prefix: str = "job_",
        create: bool = False,
    ) -> None:
        self.path = config.paths.staging_dir / f"{prefix}{uuid.uuid4().hex[:12]}"
        self._create = create
        self._lock: StagingLock | None = None

    def __enter__(self) -> Path:
        self._lock = acquire_staging_lock(self.path.parent / f".lock_{self.path.name}")
        if self._create:
            self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            shutil.rmtree(self.path, ignore_errors=True)
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None


def is_archive(path: Path) -> bool:
    """Return True if `path` has a supported archive extension (`.zip`, `.7z`, `.rar`)."""
    return path.suffix.lower() in SUPPORTED_ARCHIVE_EXTENSIONS


def _normalize_internal_path(raw_path: str) -> str:
    return raw_path.replace("\\", "/").lstrip("/")


def _list_zip_stdlib(archive_path: Path) -> tuple[ArchiveEntry, ...]:
    """Fast zero-subprocess central directory inspection for `.zip` archives."""
    entries: list[ArchiveEntry] = []
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            norm = _normalize_internal_path(info.filename).rstrip("/")
            if not norm:
                continue
            entries.append(
                ArchiveEntry(
                    path=norm,
                    size=int(info.file_size),
                    is_dir=info.is_dir(),
                )
            )
    return tuple(entries)


def _parse_7z_slt_output(stdout: str) -> tuple[ArchiveEntry, ...]:
    """Parse `7z l -slt` technical block output into `ArchiveEntry` items."""
    entries: list[ArchiveEntry] = []
    in_entries_section = False
    current: dict[str, str] = {}

    def _flush_block(block: dict[str, str]) -> None:
        raw_path = block.get("Path", "").strip()
        if not raw_path:
            return
        norm = _normalize_internal_path(raw_path).rstrip("/")
        if not norm:
            return
        folder_flag = block.get("Folder", "").strip()
        attrs = block.get("Attributes", "").strip()
        is_dir = folder_flag == "+" or attrs.upper().startswith("D")
        size_str = block.get("Size", "0").strip()
        try:
            size = int(size_str) if size_str else 0
        except ValueError:
            size = 0
        entries.append(ArchiveEntry(path=norm, size=size, is_dir=is_dir))

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("----------"):
            in_entries_section = True
            current = {}
            continue
        if not in_entries_section:
            continue
        if not line:
            if current:
                _flush_block(current)
                current = {}
            continue
        if " = " in raw_line:
            key, val = raw_line.split(" = ", 1)
            current[key.strip()] = val.strip()

    if current:
        _flush_block(current)

    return tuple(entries)


def _raise_7z_error(
    subcommand: str,
    archive_path: Path,
    seven_zip_bin: Path,
    returncode: int,
    detail: str,
) -> None:
    """Raise an informative `ArchiveError` when `7z` exits non-zero (detecting missing RAR codecs)."""
    if archive_path.suffix.lower() == ".rar" and "Cannot open the file as archive" in detail:
        if not check_seven_zip_supports_rar(seven_zip_bin):
            raise ArchiveError(
                f"`7z {subcommand}` failed on `{archive_path.name}` (exit {returncode}): {detail}\n"
                f"Root cause: `{seven_zip_bin}` lacks RAR decompression support.\n"
                "On Linux distributions like Fedora and Debian, the distribution `7zip` package is compiled "
                "without proprietary RAR codecs due to non-free licensing restrictions.\n"
                "Fix: Download official standalone `7zz` from https://www.7-zip.org/download.html "
                "and place it in `tools/7z/7zz` (or on your PATH) to enable .rar extraction."
            )
    raise ArchiveError(
        f"`7z {subcommand}` failed on `{archive_path.name}` (exit {returncode}): {detail}"
    )


def _list_via_7z(archive_path: Path, seven_zip_bin: Path | None) -> tuple[ArchiveEntry, ...]:
    """List archive metadata via `7z l -slt -sccUTF-8` without extracting."""
    if seven_zip_bin is None:
        raise ArchiveError(
            f"Cannot inspect `{archive_path.name}`: `7z` binary not found. "
            "Install 7-Zip / p7zip or configure `[binaries]` in config.toml."
        )

    cmd = [str(seven_zip_bin), "l", "-slt", "-sccUTF-8", str(archive_path)]
    try:
        proc = run_capture(cmd, timeout=120.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchiveError(f"Failed invoking 7z on `{archive_path.name}`: {exc}") from exc

    stdout = proc.stdout if isinstance(proc.stdout, str) else ""
    stderr = proc.stderr if isinstance(proc.stderr, str) else ""
    if proc.returncode != 0:
        detail = (stderr or stdout).strip()
        _raise_7z_error("l", archive_path, seven_zip_bin, proc.returncode, detail)

    return _parse_7z_slt_output(stdout)


@functools.lru_cache(maxsize=1024)
def _list_archive_entries_cached(
    resolved_path: Path,
    mtime_ns: int,
    size: int,
    seven_zip_bin: Path | None,
) -> tuple[ArchiveEntry, ...]:
    """Bounded cache (`maxsize=1024`) for archive listing keyed on `(path, mtime_ns, size, seven_zip_bin)`."""
    _ = (mtime_ns, size)
    ext = resolved_path.suffix.lower()
    if ext == ".zip":
        try:
            return _list_zip_stdlib(resolved_path)
        except (zipfile.BadZipFile, NotImplementedError, OSError):
            if seven_zip_bin is not None:
                return _list_via_7z(resolved_path, seven_zip_bin)
            raise ArchiveError(f"Corrupt or unsupported `.zip` archive: {resolved_path.name}")

    if ext in (".7z", ".rar"):
        return _list_via_7z(resolved_path, seven_zip_bin)

    raise ArchiveError(f"Unsupported archive format: {resolved_path.suffix}")


def list_archive_entries(
    archive_path: Path,
    seven_zip_bin: Path | None,
) -> tuple[ArchiveEntry, ...]:
    """Peek inside `.zip`, `.7z`, or `.rar` and return its internal file entries."""
    try:
        st = archive_path.stat()
    except OSError as exc:
        raise ArchiveError(f"Archive does not exist: {archive_path}") from exc
    if not archive_path.is_file():
        raise ArchiveError(f"Archive does not exist: {archive_path}")

    return _list_archive_entries_cached(
        archive_path.resolve(),
        st.st_mtime_ns,
        st.st_size,
        seven_zip_bin,
    )


def inspect_archive(archive_path: Path, config: PipelineConfig) -> ArchiveInspection:
    """
    Step 1 & 2 of `archive_handler`:
      1. Peek at internal file list in memory without extracting to disk.
      2. Run the Arcade heuristic & 5-way FBNeo classifier (`cps1/2/3`, `neogeo`, `fbneo`).
    """
    entries = list_archive_entries(archive_path, config.binaries.seven_zip)
    total_bytes = sum(e.size for e in entries if not e.is_dir)

    is_arcade, arcade_sys, method, reason = classify_arcade_archive(
        archive_path.stem,
        entries,
    )

    return ArchiveInspection(
        archive_path=archive_path,
        entries=entries,
        total_uncompressed_bytes=total_bytes,
        is_arcade=is_arcade,
        arcade_system_id=arcade_sys,
        detection_method=method,
        reason=reason,
    )


_WIN_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_WIN_ILLEGAL_PART_CHARS_RE = re.compile(r'[<>:"|?*]')


def _safe_host_rel_parts(posix_parts: tuple[str, ...], platform_key: str) -> tuple[str, ...]:
    """Sanitize path components for local host extraction (e.g. `:` in game titles on Windows)."""
    if platform_key != "windows":
        return posix_parts
    return tuple(_WIN_ILLEGAL_PART_CHARS_RE.sub("_", part).rstrip(" .") or "_" for part in posix_parts)


def validate_safe_entries(entries: tuple[ArchiveEntry, ...], dest_dir: Path) -> None:
    """Guard against Zip-Slip / path traversal attacks (`../../`, drive prefixes) before extraction."""
    resolved_root = dest_dir.resolve()
    for entry in entries:
        posix_parts = PurePosixPath(entry.path).parts
        if (
            not posix_parts
            or ".." in posix_parts
            or entry.path.startswith(("/", "\\"))
            or _WIN_DRIVE_PREFIX_RE.match(posix_parts[0])
        ):
            raise ArchiveError(f"Unsafe path traversal entry inside archive: {entry.path!r}")
        sanitized_parts = _safe_host_rel_parts(posix_parts, "windows" if os.name == "nt" else "linux")
        candidate = (resolved_root / Path(*sanitized_parts)).resolve()
        if not candidate.is_relative_to(resolved_root):
            raise ArchiveError(f"Archive entry escapes staging directory: {entry.path!r}")


def check_staging_space(required_uncompressed_bytes: int, config: PipelineConfig) -> None:
    """Verify `.staging` has enough free disk space for both extraction and conversion."""
    config.paths.staging_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(config.paths.staging_dir)
    min_floor_bytes = int(config.general.min_free_staging_gb * (1024**3))
    # 2x accounts for simultaneous extracted raw ISO + compressed CHD/RVZ output
    estimated_peak_bytes = int(required_uncompressed_bytes * 2.0)
    required_bytes = max(min_floor_bytes, estimated_peak_bytes)

    if usage.free < required_bytes:
        free_gib = usage.free / (1024**3)
        req_gib = required_bytes / (1024**3)
        raise InsufficientStagingSpaceError(
            f"Insufficient free space in staging ({config.paths.staging_dir}): "
            f"{free_gib:.2f} GiB available, {req_gib:.2f} GiB required."
        )


def extract_archive_to_staging(
    inspection: ArchiveInspection,
    config: PipelineConfig,
    job_id: str | None = None,
) -> Path:
    """
    Step 3 of `archive_handler`:
    Extract a non-arcade archive into `.staging/<job_uuid>/` and return the job directory.
    """
    if inspection.is_arcade:
        raise ArchiveError(
            f"Refusing to extract Arcade archive `{inspection.archive_path.name}` "
            f"(detected as `{inspection.arcade_system_id}`). Pass raw archive directly to router."
        )

    validate_safe_entries(inspection.entries, config.paths.staging_dir)
    check_staging_space(inspection.total_uncompressed_bytes, config)

    active_job_id = job_id or uuid.uuid4().hex[:12]
    job_dir = config.paths.staging_dir / active_job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    seven_zip = config.binaries.seven_zip
    ext = inspection.archive_path.suffix.lower()

    try:
        if seven_zip is not None:
            cmd = [
                str(seven_zip),
                "x",
                "-y",
                "-sccUTF-8",
                f"-o{job_dir}",
                str(inspection.archive_path),
            ]
            proc = run_capture(cmd, timeout=None)
            stdout = proc.stdout if isinstance(proc.stdout, str) else ""
            stderr = proc.stderr if isinstance(proc.stderr, str) else ""
            if proc.returncode != 0:
                detail = (stderr or stdout).strip()
                _raise_7z_error("x", inspection.archive_path, seven_zip, proc.returncode, detail)
        elif ext == ".zip":
            with zipfile.ZipFile(inspection.archive_path, "r") as zf:
                for info in zf.infolist():
                    norm = _normalize_internal_path(info.filename).rstrip("/")
                    if not norm:
                        continue
                    safe_parts = _safe_host_rel_parts(
                        PurePosixPath(norm).parts, config.platform_key
                    )
                    target_path = job_dir / Path(*safe_parts)
                    if info.is_dir():
                        target_path.mkdir(parents=True, exist_ok=True)
                    else:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info, "r") as src_f, target_path.open("wb") as dst_f:
                            shutil.copyfileobj(src_f, dst_f, length=1024 * 1024)
        else:
            raise ArchiveError(
                f"`7z` binary is required to extract `{inspection.archive_path.name}`."
            )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    return job_dir


def _should_touch_only_in_preview(
    entry: ArchiveEntry,
    is_wiiu_tree: bool,
    has_multitrack_cd_track01: bool,
    is_vita_tree: bool = False,
) -> bool:
    """
    Return True if `entry` only needs a 0-byte placeholder in header-preview staging:
      - In Wii U Loadiine/NUS trees, only `meta.xml` and `title.tmd` are parsed.
      - In PS Vita NoNpDrm/MaiDump trees, only `sce_sys/param.sfo` is parsed.
      - In multi-track CD sets that have a `.cue`/`.gdi` and a `Track 01` data file,
        secondary `Track 02..99` audio tracks only need to exist on disk so `.cue`
        resolution claims them without spawning `7z e -so` per audio track.
    """
    entry_name_lower = PurePosixPath(entry.path).name.lower()
    if is_wiiu_tree and entry_name_lower not in ("meta.xml", "title.tmd"):
        return True
    if is_vita_tree and entry_name_lower != "param.sfo":
        return True
    if has_multitrack_cd_track01 and entry.suffix in _SECONDARY_TRACK_EXTS:
        m = _TRACK_NUM_RE.search(entry.filename)
        if m and int(m.group(1)) >= 2:
            return True
    return False


def extract_header_preview_to_staging(
    inspection: ArchiveInspection,
    config: PipelineConfig,
    preview_limit_bytes: int = 16 * 1024 * 1024,
    target_dir: Path | None = None,
) -> tuple[Path, dict[Path, int]]:
    """
    Create a fast, lightweight header preview in `.staging/_inspect_<uuid>/` (or `target_dir`):
      - Small descriptor files (`<= preview_limit_bytes`, e.g. `.cue`, `.gdi`) are written in full.
      - Multi-GB disc/ROM images stream ONLY the first `preview_limit_bytes` (default 16 MiB),
        avoiding full extraction while preserving true uncompressed sizes in `logical_sizes`.
      - Secondary CD audio tracks (`Track 02..99`) and non-metadata Wii U tree files are created
        as 0-byte placeholders so `7z` is never spawned 50+ times on Redbook CD-DA archives.
    """
    validate_safe_entries(inspection.entries, config.paths.staging_dir)
    preview_dir = (
        target_dir
        if target_dir is not None
        else (config.paths.staging_dir / f"_inspect_{uuid.uuid4().hex[:10]}")
    )
    preview_dir.mkdir(parents=True, exist_ok=True)
    logical_sizes: dict[Path, int] = {}

    ext = inspection.archive_path.suffix.lower()
    seven_zip = config.binaries.seven_zip
    file_entries = inspection.file_entries

    is_wiiu_tree = any(
        PurePosixPath(e.path).name.lower() in ("meta.xml", "title.tmd")
        or PurePosixPath(e.path).suffix.lower() == ".rpx"
        for e in file_entries
    )
    is_vita_tree = any(
        PurePosixPath(e.path).name.lower() == "param.sfo"
        and len(PurePosixPath(e.path).parts) >= 2
        and PurePosixPath(e.path).parts[-2].lower() == "sce_sys"
        for e in file_entries
    )
    has_cd_descriptor = any(e.suffix in (".cue", ".gdi") for e in file_entries)
    has_track01 = any(
        e.suffix in _SECONDARY_TRACK_EXTS
        and (m := _TRACK_NUM_RE.search(e.filename)) is not None
        and int(m.group(1)) == 1
        for e in file_entries
    )
    has_multitrack_cd_track01 = has_cd_descriptor and has_track01

    try:
        if ext == ".zip":
            try:
                with zipfile.ZipFile(inspection.archive_path, "r") as zf:
                    for entry in file_entries:
                        out_path = preview_dir / Path(
                            *_safe_host_rel_parts(PurePosixPath(entry.path).parts, config.platform_key)
                        )
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        if _should_touch_only_in_preview(
                            entry, is_wiiu_tree, has_multitrack_cd_track01, is_vita_tree
                        ):
                            out_path.touch()
                            logical_sizes[out_path.resolve()] = entry.size
                            continue
                        with zf.open(entry.path, "r") as src, out_path.open("wb") as dst:
                            remaining = preview_limit_bytes
                            while remaining > 0:
                                chunk = src.read(min(262144, remaining))
                                if not chunk:
                                    break
                                dst.write(chunk)
                                remaining -= len(chunk)
                        logical_sizes[out_path.resolve()] = entry.size
                return preview_dir, logical_sizes
            except (zipfile.BadZipFile, NotImplementedError):
                pass  # Fall through to 7z stream below

        if seven_zip is None:
            raise ArchiveError(
                f"`7z` is required to stream headers from `{inspection.archive_path.name}`."
            )

        for entry in file_entries:
            out_path = preview_dir / Path(
                *_safe_host_rel_parts(PurePosixPath(entry.path).parts, config.platform_key)
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if _should_touch_only_in_preview(
                entry, is_wiiu_tree, has_multitrack_cd_track01, is_vita_tree
            ):
                out_path.touch()
                logical_sizes[out_path.resolve()] = entry.size
                continue
            # Normalize path separator for 7z filter matching on Windows
            internal_arg = entry.path.replace("/", "\\") if config.platform_key == "windows" else entry.path
            cmd = [
                str(seven_zip),
                "e",
                "-so",
                "-spd",
                "-y",
                "-sccUTF-8",
                str(inspection.archive_path),
                internal_arg,
            ]
            stream_stdout_slice(cmd, out_path, preview_limit_bytes)
            logical_sizes[out_path.resolve()] = entry.size

        return preview_dir, logical_sizes
    except Exception:
        shutil.rmtree(preview_dir, ignore_errors=True)
        raise
