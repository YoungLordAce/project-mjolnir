"""Watchdog directory monitor, stability/lock verifier, and sequential queue worker."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
from typing import Callable

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.archive_handler import (
    ArchiveError,
    InsufficientStagingSpaceError,
    StagingLock,
    acquire_staging_lock,
    is_staging_lock_held,
)
from src.config_loader import ensure_local_directories
from src.constants import (
    IGNORED_FILENAMES,
    IGNORED_SIDECAR_EXTENSIONS,
    SIBLING_TEMP_MARKER_SUFFIXES,
    SUPPORTED_ARCHIVE_EXTENSIONS,
    TEMP_DOWNLOAD_SUFFIXES,
    WATCH_PLATFORM_BUCKETS,
    is_syncthing_temp,
)
from src.converter import ConversionError
from src.identifier import (
    AMBIGUOUS_DISC_EXTENSIONS,
    UNAMBIGUOUS_EXTENSION_MAP,
    _find_pkg_license_companions,
    parse_cue_tracks,
    parse_gdi_tracks,
    sanitize_rom_stem,
)
from src.models import PipelineConfig, RoutingOutcome
from src.router import (
    RoutingError,
    execute_ingestion_batch,
    group_ingestion_batches,
    probe_multidisc_source_title,
)
from src.runner import terminate_active_subprocesses
from src.switch_bundler import (
    SwitchContentRole,
    SwitchSourceInfo,
    _normalize_switch_title_key,
    find_nas_switch_family_files,
    group_candidates_with_switch_families,
    probe_switch_source,
)


def _platform_bucket_names(config: PipelineConfig | None = None) -> frozenset[str]:
    if config is None:
        return WATCH_PLATFORM_BUCKETS
    extra = {s.folder.lower() for s in config.systems.values()} | set(config.systems.keys())
    return WATCH_PLATFORM_BUCKETS | extra


def _is_watch_root_or_bucket_dir(
    directory: Path,
    watch_root: Path,
    platform_buckets: frozenset[str],
) -> bool:
    """Return True if `directory` is `watch_root` or a 1-level ROMarr platform bucket inside `watch_root`."""
    return directory == watch_root or (
        directory.parent == watch_root and directory.name.lower() in platform_buckets
    )


def _expand_romarr_platform_bucket(
    entry: Path,
    watch_root: Path,
    platform_buckets: frozenset[str],
) -> list[Path] | None:
    """
    If `entry` is a top-level ROMarr platform bucket directory (`drop/gba`, `drop/switch`, etc.),
    return its actionable children (resolving `.bin` tracks to their claiming `.cue`/`.gdi`).
    Otherwise return `None`.
    """
    resolved = entry.resolve()
    if (
        not resolved.is_dir()
        or resolved.parent != watch_root
        or resolved.name.startswith((".", "~syncthing~"))
        or resolved.name.lower() not in platform_buckets
    ):
        return None

    bucket_candidates: list[Path] = []
    bucket_seen: set[Path] = set()
    _collect_candidates_in_dir(resolved, bucket_candidates, bucket_seen, platform_buckets=None)
    return bucket_candidates


VALID_TARGET_EXTENSIONS: frozenset[str] = (
    SUPPORTED_ARCHIVE_EXTENSIONS
    | frozenset(UNAMBIGUOUS_EXTENSION_MAP.keys())
    | AMBIGUOUS_DISC_EXTENSIONS
    | frozenset({".cue", ".gdi"})
)


def is_temp_download_file(path: Path) -> bool:
    """Return True if `path` is an active temporary browser/downloader/Syncthing artifact."""
    if is_syncthing_temp(path.name):
        return True
    return path.suffix.lower() in TEMP_DOWNLOAD_SUFFIXES


def is_ignored_metadata_file(path: Path) -> bool:
    """Return True for OS metadata placeholders and downloader sidecars (e.g. `.gamarr.json`)."""
    name_lower = path.name.lower()
    return name_lower in IGNORED_FILENAMES or name_lower.endswith(".gamarr.json")


def has_sibling_temp_marker(file_path: Path) -> bool:
    """
    Detect companion temp files created alongside placeholders (e.g. Firefox creates
    0-byte `Game.zip` + active `Game.zip.part` simultaneously).
    """
    parent = file_path.parent
    base = file_path.name
    for temp_ext in SIBLING_TEMP_MARKER_SUFFIXES:
        if (parent / f"{base}{temp_ext}").exists():
            return True
    return False


def can_open_exclusively(file_path: Path) -> bool:
    """
    Verify the OS/browser/downloader has completely released its write handle on `file_path`.

    - Windows 11: Uses Win32 `CreateFileW` with `dwShareMode = 0` (zero handle sharing),
      which fails immediately at the kernel level if ANY process still holds the file open.
    - Fedora Linux: Opens `O_RDWR` and acquires a non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)`.
    """
    if not file_path.is_file():
        return False

    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        get_attrs = kernel32.GetFileAttributesW
        get_attrs.argtypes = [wintypes.LPCWSTR]
        get_attrs.restype = wintypes.DWORD

        generic_read = 0x80000000
        no_share = 0x00000000
        open_existing = 3
        file_attr_normal = 0x80
        invalid_handle = wintypes.HANDLE(-1).value

        attrs = get_attrs(str(file_path))
        if attrs == 0xFFFFFFFF:
            return False

        # Opening with `GENERIC_READ` and `dwShareMode = 0` (`no_share`) fails at the Win32
        # kernel level if ANY other process holds a read, write, or delete handle on `file_path`,
        # without triggering a spurious `FILE_ACTION_MODIFIED` event in `ReadDirectoryChangesW`.
        handle = create_file(
            str(file_path),
            generic_read,
            no_share,
            None,
            open_existing,
            file_attr_normal,
            None,
        )
        if handle == invalid_handle or handle is None or handle == 0:
            return False
        close_handle(handle)
        return True

    # Linux / POSIX
    import fcntl

    fd: int | None = None
    try:
        try:
            fd = os.open(file_path, os.O_RDWR)
        except PermissionError as exc:
            if exc.errno == errno.EACCES and not os.access(file_path, os.W_OK):
                fd = os.open(file_path, os.O_RDONLY)
            else:
                return False
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def find_claiming_descriptor_in_dir(track_file: Path, directory: Path) -> Path | None:
    """
    If `track_file` is a loose `.bin`/`.raw`/`.img` inside `directory`, return the `.cue`,
    `.gdi`, or `.pkg` (`work.bin`) in `directory` that references it (if any), preventing
    premature companion queueing.
    """
    if track_file.suffix.lower() not in (".bin", ".raw", ".img", ".iso", ".wav"):
        return None
    resolved_track = track_file.resolve()
    is_work_bin = track_file.name.lower() == "work.bin"
    try:
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext == ".cue":
                if any(t.resolve() == resolved_track for t in parse_cue_tracks(entry)):
                    return entry.resolve()
            elif ext == ".gdi":
                if any(t.resolve() == resolved_track for t in parse_gdi_tracks(entry)):
                    return entry.resolve()
            elif ext == ".pkg" and is_work_bin:
                return entry.resolve()
    except OSError:
        pass
    return None


def probe_target_snapshot(target: Path) -> tuple[bool, tuple[int, int]]:
    """
    Inspect `target` (file or folder) for stability polling.

    Returns `(is_unlocked_and_valid, (file_count, total_bytes))`.
    """
    if not target.exists():
        return False, (0, 0)

    # Case 1: Top-level file
    if target.is_file():
        if is_temp_download_file(target) or is_ignored_metadata_file(target):
            return False, (0, 0)
        if has_sibling_temp_marker(target):
            return False, (0, 0)

        ext = target.suffix.lower()
        if ext not in VALID_TARGET_EXTENSIONS:
            return False, (0, 0)

        # Loose `.cue` or `.gdi` must also validate all referenced companion `.bin` tracks
        if ext in (".cue", ".gdi"):
            tracks = parse_cue_tracks(target) if ext == ".cue" else parse_gdi_tracks(target)
            if not tracks:
                return False, (0, 0)
            all_parts = [target, *tracks]
            total_bytes = 0
            for part in all_parts:
                try:
                    sz = part.stat().st_size
                except OSError:
                    return False, (0, 0)
                if sz <= 0 or has_sibling_temp_marker(part) or not can_open_exclusively(part):
                    return False, (len(all_parts), total_bytes)
                total_bytes += sz
            return True, (len(all_parts), total_bytes)

        if ext == ".pkg":
            all_parts = [target, *_find_pkg_license_companions(target)]
            total_bytes = 0
            for part in all_parts:
                try:
                    sz = part.stat().st_size
                except OSError:
                    return False, (0, 0)
                if sz <= 0 or has_sibling_temp_marker(part) or not can_open_exclusively(part):
                    return False, (len(all_parts), total_bytes)
                total_bytes += sz
            return True, (len(all_parts), total_bytes)

        try:
            size = target.stat().st_size
        except OSError:
            return False, (0, 0)
        if size <= 0 or not can_open_exclusively(target):
            return False, (1, size)
        return True, (1, size)

    # Case 2: Top-level dropped folder
    if target.is_dir():
        if target.name.startswith((".", "~syncthing~")):
            return False, (0, 0)

        total_bytes = 0
        actionable_count = 0
        file_count = 0
        try:
            children = list(target.rglob("*"))
        except OSError:
            return False, (0, 0)

        for child in children:
            if not child.is_file():
                continue
            if is_ignored_metadata_file(child):
                continue
            # Any active temp file inside the folder means download/copy is still in progress
            if is_temp_download_file(child) or has_sibling_temp_marker(child):
                return False, (0, 0)

            try:
                sz = child.stat().st_size
            except OSError:
                return False, (0, 0)
            if not can_open_exclusively(child):
                return False, (file_count, total_bytes)

            file_count += 1
            total_bytes += sz
            cext = child.suffix.lower()
            cname = child.name.lower()
            if (
                cext in VALID_TARGET_EXTENSIONS
                or cext == ".rpx"
                or cname in ("param.sfo", "title.tmd")
            ) and cext not in IGNORED_SIDECAR_EXTENSIONS:
                actionable_count += 1

        if actionable_count == 0 or total_bytes <= 0:
            return False, (file_count, total_bytes)
        return True, (file_count, total_bytes)

    return False, (0, 0)


def purge_stale_staging(config: PipelineConfig) -> int:
    """
    Remove leftover `job_*` or `_inspect_*` directories in `.staging/` on startup,
    while preserving any directory protected by an active watcher or job lockfile.
    """
    staging = config.paths.staging_dir
    if not staging.is_dir():
        return 0

    if is_staging_lock_held(staging / ".lock_watcher"):
        return 0

    active_job_names: set[str] = set()
    for child in staging.iterdir():
        if child.name.startswith(".lock_") and is_staging_lock_held(child):
            active_job_names.add(child.name.removeprefix(".lock_"))

    removed = 0
    for child in staging.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.name.startswith(".lock_"):
            if child.name.removeprefix(".lock_") in active_job_names:
                continue
            child.unlink(missing_ok=True)
            continue
        if child.name in active_job_names:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        elif child.is_file():
            child.unlink(missing_ok=True)
            removed += 1
    return removed


def _collect_candidates_in_dir(
    scan_dir: Path,
    candidates: list[Path],
    seen: set[Path],
    platform_buckets: frozenset[str] | None = None,
) -> None:
    """Collect actionable files/folders inside `scan_dir`, descending into known platform buckets."""
    raw_entries = sorted(scan_dir.iterdir(), key=lambda p: p.name.lower())
    for entry in raw_entries:
        resolved = entry.resolve()
        if resolved in seen:
            continue
        if is_ignored_metadata_file(entry) or is_temp_download_file(entry):
            continue

        if entry.is_file():
            if entry.suffix.lower() not in VALID_TARGET_EXTENSIONS:
                continue
            owner_desc = find_claiming_descriptor_in_dir(entry, scan_dir)
            if owner_desc is not None:
                if owner_desc not in seen:
                    seen.add(owner_desc)
                    candidates.append(owner_desc)
                continue
            seen.add(resolved)
            candidates.append(resolved)
        elif entry.is_dir() and not entry.name.startswith((".", "~syncthing~")):
            if platform_buckets is not None:
                bucket_children = _expand_romarr_platform_bucket(
                    entry,
                    scan_dir.resolve(),
                    platform_buckets,
                )
                if bucket_children is not None:
                    for child in bucket_children:
                        if child not in seen:
                            seen.add(child)
                            candidates.append(child)
                    continue
            seen.add(resolved)
            candidates.append(resolved)


def discover_watch_candidates(
    watch_dir: Path,
    config: PipelineConfig | None = None,
) -> list[Path]:
    """List actionable top-level files/folders (and ROMarr platform-bucket children) in `watch_dir`."""
    if not watch_dir.is_dir():
        return []

    candidates: list[Path] = []
    seen: set[Path] = set()
    buckets = _platform_bucket_names(config)
    _collect_candidates_in_dir(watch_dir, candidates, seen, platform_buckets=buckets)
    return candidates


class _StabilityState:
    """Per-item state tracked across consecutive stability polls."""

    __slots__ = ("last_signature", "stable_hits")

    def __init__(self) -> None:
        self.last_signature: tuple[int, int] | None = None
        self.stable_hits: int = 0


class _WatchdogDropHandler(FileSystemEventHandler):
    """Non-blocking Watchdog event listener forwarding candidate paths to `PipelineWatcher`."""

    def __init__(
        self,
        notify_callback: Callable[[Path], None],
        watch_root: Path,
        platform_buckets: frozenset[str] = WATCH_PLATFORM_BUCKETS,
    ) -> None:
        super().__init__()
        self._notify = notify_callback
        self._watch_root = watch_root.resolve()
        self._platform_buckets = platform_buckets

    def _handle_raw_path(self, raw_path: str | bytes) -> None:
        path_str = os.fsdecode(raw_path)
        try:
            resolved = Path(path_str).resolve()
            rel = resolved.relative_to(self._watch_root)
        except (OSError, ValueError):
            return
        if not rel.parts:
            return

        if rel.parts[0].lower() in self._platform_buckets:
            if len(rel.parts) < 2:
                return
            top_item = self._watch_root / rel.parts[0] / rel.parts[1]
        else:
            top_item = self._watch_root / rel.parts[0]
        if is_ignored_metadata_file(top_item) or is_temp_download_file(top_item):
            return
        self._notify(top_item)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_raw_path(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_raw_path(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        # Crucial for Chrome/Firefox/Edge renaming `.crdownload`/`.part` -> `.zip`/`.7z`!
        self._handle_raw_path(event.dest_path)


def has_active_downloads_in_dir(watch_dir: Path) -> bool:
    """Return True if any `.crdownload`/`.part`/`.tmp` incomplete download exists in `watch_dir`."""
    if not watch_dir.is_dir():
        return False
    try:
        for entry in watch_dir.iterdir():
            if is_temp_download_file(entry):
                return True
            if entry.is_dir() and not entry.name.startswith((".", "~syncthing~")):
                for sub in entry.rglob("*"):
                    if is_temp_download_file(sub):
                        return True
    except OSError:
        return False
    return False


class PipelineWatcher:
    """
    Coordinates non-blocking `watchdog` filesystem monitoring, multi-poll stability +
    exclusive lock checks, Switch TitleID sibling bundling, and sequential worker execution.
    """

    def __init__(
        self,
        config: PipelineConfig,
        on_event_log: Callable[[str], None] | None = None,
        on_outcome: Callable[[RoutingOutcome], None] | None = None,
        on_error: Callable[[Path, Exception], None] | None = None,
    ) -> None:
        self.config = config
        self._log = on_event_log or (lambda _msg: None)
        self._on_outcome = on_outcome or (lambda _out: None)
        self._on_error = on_error or (lambda _p, _e: None)

        self.job_queue: queue.Queue[tuple[str, tuple[Path, ...]] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._pending_stability: dict[Path, _StabilityState] = {}
        self._ready_buffer: list[Path] = []
        self._ready_signatures: dict[Path, tuple[int, int]] = {}
        self._queued_or_active: set[Path] = set()
        self._logged_hold_keys: set[str] = set()

        self._stop_event = threading.Event()
        self._observer: Observer | None = None
        self._poller_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._watcher_lock: StagingLock | None = None
        self._platform_buckets = _platform_bucket_names(config)

        # Throttled NAS re-check state for held orphan Switch Update/DLC items:
        # `fam_id -> (last_switch_dir_mtime_ns, last_check_monotonic)`
        self._orphan_nas_recheck_seconds: float = 30.0
        self._orphan_nas_check_state: dict[str, tuple[int | None, float]] = {}

        # Bounded retry/backoff state for transient NAS/staging errors
        self._retry_backoffs: tuple[float, ...] = (15.0, 60.0, 300.0)
        self._retry_counts: dict[tuple[Path, ...], int] = {}
        self._retry_schedule: dict[tuple[Path, ...], float] = {}

    def register_candidate(self, candidate: Path) -> None:
        """Register a top-level file/folder (or platform-bucket child) in `watch_dir` for stability polling."""
        resolved = candidate.resolve()
        watch_root = self.config.paths.watch_dir.resolve()

        # If a ROMarr platform bucket directory itself is passed, register its children individually
        bucket_children = _expand_romarr_platform_bucket(resolved, watch_root, self._platform_buckets)
        if bucket_children is not None:
            for child in bucket_children:
                self.register_candidate(child)
            return

        parent_dir = resolved.parent
        # If a loose `.bin` track arrives alongside a `.cue`/`.gdi`, track the descriptor instead
        if _is_watch_root_or_bucket_dir(parent_dir, watch_root, self._platform_buckets) and resolved.is_file():
            owner = find_claiming_descriptor_in_dir(resolved, parent_dir)
            if owner is not None:
                resolved = owner
            elif resolved.suffix.lower() not in VALID_TARGET_EXTENSIONS:
                return

        needs_recheck = False
        prev_sig: tuple[int, int] | None = None
        with self._lock:
            if resolved in self._queued_or_active:
                return
            if resolved in self._ready_buffer:
                needs_recheck = True
                prev_sig = self._ready_signatures.get(resolved)
            else:
                if resolved not in self._pending_stability:
                    self._pending_stability[resolved] = _StabilityState()
                    self._log(f"Detected new item (awaiting stability): {resolved.name}")
                return

        if needs_recheck:
            ready_now, sig_now = probe_target_snapshot(resolved)
            if ready_now and prev_sig is not None and sig_now == prev_sig:
                # Ignore spurious filesystem notifications when the stabilized signature is unchanged
                return
            with self._lock:
                if resolved in self._queued_or_active:
                    return
                if resolved in self._ready_buffer:
                    self._ready_buffer.remove(resolved)
                self._ready_signatures.pop(resolved, None)
                if resolved not in self._pending_stability:
                    self._pending_stability[resolved] = _StabilityState()
                    self._log(f"Detected new item (awaiting stability): {resolved.name}")

    def _flush_ready_buffer(self, watch_root: Path) -> None:
        """
        Dispatch stabilized items to `job_queue`:
          - Non-Switch items queue immediately with zero extra wait.
          - Switch TitleID family items wait if sibling downloads are still active in `watch_dir`
            or if an Update/DLC has no Base game in `watch_dir` or on NAS yet.
        """
        with self._lock:
            raw_ready = list(self._ready_buffer)
            has_pending = bool(self._pending_stability)

        dead_paths: set[Path] = set()
        for p in raw_ready:
            try:
                if not p.exists():
                    dead_paths.add(p)
            except OSError:
                dead_paths.add(p)

        with self._lock:
            alive_ready: list[Path] = []
            for p in self._ready_buffer:
                if p in dead_paths or p in self._queued_or_active:
                    if p in dead_paths:
                        self._ready_signatures.pop(p, None)
                    continue
                alive_ready.append(p)
            self._ready_buffer = alive_ready
            # Prune any orphaned signature entries no longer in `_ready_buffer`
            ready_set = set(alive_ready)
            for sig_path in list(self._ready_signatures):
                if sig_path not in ready_set:
                    self._ready_signatures.pop(sig_path, None)
            ready_snapshot = list(alive_ready)
            if not ready_snapshot:
                self._logged_hold_keys.clear()
                self._orphan_nas_check_state.clear()

        if not ready_snapshot:
            return

        active_downloads = has_pending or has_active_downloads_in_dir(watch_root)
        switch_buckets: dict[str, list[tuple[Path, SwitchSourceInfo]]] = {}
        untagged_switch_items: list[tuple[Path, SwitchSourceInfo]] = []
        multidisc_buckets: dict[str, tuple[str, list[tuple[int, Path]]]] = {}
        immediate_singles: list[Path] = []

        for p in ready_snapshot:
            info = (
                probe_switch_source(p, self.config)
                if "switch" in self.config.systems
                else None
            )
            if info is not None:
                if info.primary_family_id is not None:
                    switch_buckets.setdefault(info.primary_family_id, []).append((p, info))
                else:
                    untagged_switch_items.append((p, info))
                continue

            md_info = probe_multidisc_source_title(p)
            if md_info is not None:
                base_title, disc_num = md_info
                key = base_title.lower()
                if key not in multidisc_buckets:
                    multidisc_buckets[key] = (base_title, [])
                multidisc_buckets[key][1].append((disc_num, p))
                continue

            immediate_singles.append(p)

        # Secondary title-stem association: pair untagged Switch sources (e.g. No-Intro `.xci`
        # inside `.7z`) with any TitleID-bearing sibling bucket sharing the same clean game title.
        remaining_untagged_switch: list[tuple[Path, SwitchSourceInfo]] = []
        for u_path, u_info in untagged_switch_items:
            raw_u_name = u_path.name if u_path.is_dir() else u_path.stem
            u_stem = sanitize_rom_stem(raw_u_name).lower()
            u_key = _normalize_switch_title_key(raw_u_name)
            matched_fam: str | None = None
            if u_stem or u_key:
                for fam_id, bucket_pairs in switch_buckets.items():
                    for bp, _ in bucket_pairs:
                        b_raw = bp.name if bp.is_dir() else bp.stem
                        if u_stem and sanitize_rom_stem(b_raw).lower() == u_stem:
                            matched_fam = fam_id
                            break
                        if u_key and _normalize_switch_title_key(b_raw) == u_key:
                            matched_fam = fam_id
                            break
                    if matched_fam is not None:
                        break
            if matched_fam is not None:
                augmented = SwitchSourceInfo(
                    path=u_info.path,
                    family_ids=(matched_fam,),
                    roles=u_info.roles or frozenset({SwitchContentRole.BASE}),
                    title_ids=u_info.title_ids or (matched_fam,),
                    package_count=u_info.package_count,
                )
                switch_buckets[matched_fam].append((u_path, augmented))
            else:
                remaining_untagged_switch.append((u_path, u_info))

        # Queue non-Switch single items immediately
        for p in immediate_singles:
            with self._lock:
                if p in self._ready_buffer and p not in self._queued_or_active:
                    self._ready_buffer.remove(p)
                    self._ready_signatures.pop(p, None)
                    self._queued_or_active.add(p)
                    self._log(f"Queued: {p.name}")
                    self.job_queue.put(("single", (p,)))

        untagged_hold_keys: set[str] = set()
        for u_path, _ in remaining_untagged_switch:
            raw_u_name = u_path.name if u_path.is_dir() else u_path.stem
            u_key = _normalize_switch_title_key(raw_u_name) or u_path.name.lower()
            untagged_hold_keys.add(u_key)
            if active_downloads:
                hold_key = f"untagged_sw:{u_key}"
                if hold_key not in self._logged_hold_keys:
                    self._logged_hold_keys.add(hold_key)
                    self._log(
                        f"Holding Switch item `{u_path.name}` while other downloads in "
                        f"`{watch_root.name}/` finish..."
                    )
                continue
            with self._lock:
                if u_path in self._ready_buffer and u_path not in self._queued_or_active:
                    self._ready_buffer.remove(u_path)
                    self._ready_signatures.pop(u_path, None)
                    self._queued_or_active.add(u_path)
                    self._logged_hold_keys.discard(f"untagged_sw:{u_key}")
                    self._log(f"Queued: {u_path.name}")
                    self.job_queue.put(("single", (u_path,)))

        # Prune stale hold/poll tracking keys for items that were deleted or moved out of `watch_dir`
        for hk in list(self._logged_hold_keys):
            if hk.startswith("md_active:") and hk.removeprefix("md_active:") not in multidisc_buckets:
                self._logged_hold_keys.discard(hk)
            elif hk.startswith("active:") and hk.removeprefix("active:") not in switch_buckets:
                self._logged_hold_keys.discard(hk)
            elif hk.startswith("orphan:") and hk.removeprefix("orphan:") not in switch_buckets:
                self._logged_hold_keys.discard(hk)
            elif hk.startswith("untagged_sw:") and hk.removeprefix("untagged_sw:") not in untagged_hold_keys:
                self._logged_hold_keys.discard(hk)
        for fam_key in list(self._orphan_nas_check_state):
            if fam_key not in switch_buckets:
                self._orphan_nas_check_state.pop(fam_key, None)

        # Flush sibling `(Disc 1..N)` archives/files once active downloads in `watch_dir` settle
        for md_key, (base_title, disc_pairs) in multidisc_buckets.items():
            if active_downloads:
                hold_key = f"md_active:{md_key}"
                if hold_key not in self._logged_hold_keys:
                    self._logged_hold_keys.add(hold_key)
                    self._log(
                        f"Holding multi-disc item(s) `{base_title}` while other downloads in "
                        f"`{watch_root.name}/` finish..."
                    )
                continue

            ordered_md = [
                p for _, p in sorted(disc_pairs, key=lambda pair: (pair[0], pair[1].name.lower()))
            ]
            with self._lock:
                still_md = [
                    p for p in ordered_md
                    if p in self._ready_buffer and p not in self._queued_or_active
                ]
                if len(still_md) != len(ordered_md):
                    continue
                for p in still_md:
                    self._ready_buffer.remove(p)
                    self._ready_signatures.pop(p, None)
                    self._queued_or_active.add(p)
                self._logged_hold_keys.discard(f"md_active:{md_key}")
                if len(still_md) >= 2:
                    self._log(f"Queued: Multi-disc set `{base_title}` ({len(still_md)} discs)")
                    self.job_queue.put(("multidisc", tuple(still_md)))
                else:
                    self._log(f"Queued: {still_md[0].name}")
                    self.job_queue.put(("single", (still_md[0],)))

        for fam_id, pairs in switch_buckets.items():
            if active_downloads:
                hold_key = f"active:{fam_id}"
                if hold_key not in self._logged_hold_keys:
                    self._logged_hold_keys.add(hold_key)
                    self._log(
                        f"Holding Switch item(s) [{fam_id}] while other downloads in "
                        f"`{watch_root.name}/` finish..."
                    )
                continue

            has_base = any(info.has_base for _, info in pairs)
            if not has_base and "switch" in self.config.systems:
                now = time.monotonic()
                try:
                    switch_dir = self.config.system_dest_dir("switch")
                    cur_dir_mtime = switch_dir.stat().st_mtime_ns if switch_dir.is_dir() else None
                except OSError:
                    switch_dir = None
                    cur_dir_mtime = None

                prev_check = self._orphan_nas_check_state.get(fam_id)
                if switch_dir is None:
                    has_base = False
                elif (
                    prev_check is not None
                    and prev_check[0] == cur_dir_mtime
                    and (now - prev_check[1]) < self._orphan_nas_recheck_seconds
                ):
                    has_base = False
                else:
                    title_hints: list[str] = []
                    for p_item, _ in pairs:
                        raw_item_name = p_item.name if p_item.is_dir() else p_item.stem
                        cleaned_hint = sanitize_rom_stem(raw_item_name)
                        if cleaned_hint and cleaned_hint not in title_hints:
                            title_hints.append(cleaned_hint)
                    try:
                        donor = None
                        for hint in (title_hints or [None]):
                            donor, _ = find_nas_switch_family_files(
                                switch_dir,
                                fam_id,
                                self.config,
                                game_title_hint=hint,
                            )
                            if donor is not None:
                                break
                        has_base = donor is not None
                    except OSError:
                        has_base = False
                    self._orphan_nas_check_state[fam_id] = (cur_dir_mtime, now)

            if not has_base:
                hold_key = f"orphan:{fam_id}"
                if hold_key not in self._logged_hold_keys:
                    self._logged_hold_keys.add(hold_key)
                    names = ", ".join(p.name for p, _ in pairs)
                    self._log(
                        f"Holding Switch Update/DLC [{fam_id}] ({names}) — "
                        "waiting for Base game to arrive..."
                    )
                continue

            grouped = group_candidates_with_switch_families([p for p, _ in pairs], self.config)
            ordered_paths = grouped[0] if grouped else [p for p, _ in pairs]
            with self._lock:
                still_present = [
                    p for p in ordered_paths
                    if p in self._ready_buffer and p not in self._queued_or_active
                ]
                if len(still_present) != len(ordered_paths):
                    continue
                for p in still_present:
                    self._ready_buffer.remove(p)
                    self._ready_signatures.pop(p, None)
                    self._queued_or_active.add(p)
                self._logged_hold_keys.discard(f"active:{fam_id}")
                self._logged_hold_keys.discard(f"orphan:{fam_id}")
                self._orphan_nas_check_state.pop(fam_id, None)
                label = (
                    still_present[0].name
                    if len(still_present) == 1
                    else f"Switch family [{fam_id}] ({len(still_present)} items)"
                )
                self._log(f"Queued: {label}")
                b_kind = "switch" if len(still_present) > 1 else "single"
                self.job_queue.put((b_kind, tuple(still_present)))

    def _enqueue_due_retries(self) -> None:
        """Re-register batches whose transient-error backoff window has elapsed."""
        now = time.monotonic()
        due_batches: list[tuple[Path, ...]] = []
        with self._lock:
            for batch_key, retry_at in list(self._retry_schedule.items()):
                if now >= retry_at:
                    self._retry_schedule.pop(batch_key, None)
                    due_batches.append(batch_key)

        for batch_key in due_batches:
            alive_paths: list[Path] = []
            for p in batch_key:
                try:
                    if p.exists():
                        alive_paths.append(p)
                except OSError:
                    continue
            if not alive_paths:
                with self._lock:
                    self._retry_counts.pop(batch_key, None)
                continue
            for p in alive_paths:
                self.register_candidate(p)

    def _poll_stability_once(self, watch_root: Path, checks_needed: int) -> None:
        """Execute a single stability-poll + ready-buffer flush iteration."""
        self._enqueue_due_retries()

        with self._lock:
            snapshot_items = list(self._pending_stability.items())

        for path, state in snapshot_items:
            if self._stop_event.is_set():
                break
            try:
                exists_now = path.exists()
            except OSError:
                exists_now = False
            if not exists_now:
                with self._lock:
                    self._pending_stability.pop(path, None)
                continue

            # Re-check if a `.cue`/`.gdi` appeared after its `.bin` started copying
            if (
                _is_watch_root_or_bucket_dir(path.parent, watch_root, self._platform_buckets)
                and path.is_file()
            ):
                owner = find_claiming_descriptor_in_dir(path, path.parent)
                if owner is not None and owner != path:
                    with self._lock:
                        self._pending_stability.pop(path, None)
                        if (
                            owner not in self._queued_or_active
                            and owner not in self._ready_buffer
                            and owner not in self._pending_stability
                        ):
                            self._pending_stability[owner] = _StabilityState()
                    continue

            ready, sig = probe_target_snapshot(path)
            if not ready:
                state.last_signature = None
                state.stable_hits = 0
                continue

            if state.last_signature == sig:
                state.stable_hits += 1
            else:
                state.last_signature = sig
                state.stable_hits = 1

            if state.stable_hits >= checks_needed:
                with self._lock:
                    self._pending_stability.pop(path, None)
                    if path not in self._queued_or_active and path not in self._ready_buffer:
                        self._ready_buffer.append(path)
                        self._ready_signatures[path] = sig
                        self._log(
                            f"Stability & lock verified ({checks_needed} checks, "
                            f"{sig[1] / (1024**2):.2f} MiB): {path.name}"
                        )

        if not self._stop_event.is_set():
            self._flush_ready_buffer(watch_root)

    def _stability_poll_loop(self) -> None:
        poll_sec = self.config.general.stability_poll_seconds
        checks_needed = self.config.general.stability_checks_required
        watch_root = self.config.paths.watch_dir.resolve()

        while not self._stop_event.is_set():
            try:
                self._poll_stability_once(watch_root, checks_needed)
            except Exception as exc:
                self._log(f"Stability poller error (continuing): {exc}")
                try:
                    self._on_error(watch_root, exc)
                except Exception:
                    pass
            self._stop_event.wait(poll_sec)

    def _schedule_transient_retry(self, batch: tuple[Path, ...], primary: Path) -> None:
        """Schedule bounded backoff retry for transient NAS/staging errors."""
        if self._stop_event.is_set():
            return
        try:
            still_exists = any(p.exists() for p in batch)
        except OSError:
            still_exists = False
        if not still_exists:
            with self._lock:
                self._retry_counts.pop(batch, None)
                self._retry_schedule.pop(batch, None)
            return

        with self._lock:
            attempt = self._retry_counts.get(batch, 0) + 1
            if attempt > len(self._retry_backoffs):
                self._retry_counts.pop(batch, None)
                self._retry_schedule.pop(batch, None)
                self._log(
                    f"Exhausted {len(self._retry_backoffs)} transient retries for `{primary.name}`; "
                    "leaving in drop folder."
                )
                return
            delay = self._retry_backoffs[attempt - 1]
            self._retry_counts[batch] = attempt
            self._retry_schedule[batch] = time.monotonic() + delay
            self._log(
                f"Transient error on `{primary.name}` — scheduling retry "
                f"{attempt}/{len(self._retry_backoffs)} in {delay:.0f}s..."
            )

    def _worker_loop(self) -> None:
        """Single sequential worker processing heavy conversions one at a time."""
        while not self._stop_event.is_set():
            try:
                queued_entry = self.job_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if queued_entry is None:
                self.job_queue.task_done()
                break

            batch_kind, batch = queued_entry
            primary = batch[0]
            try:
                existing = [p for p in batch if p.exists()]
                if not existing:
                    with self._lock:
                        self._retry_counts.pop(batch, None)
                        self._retry_schedule.pop(batch, None)
                    continue
                primary = existing[0]
                if batch_kind == "switch" and len(existing) > 1:
                    names = " + ".join(p.name for p in existing)
                    self._log(f"Processing Switch bundle ({len(existing)} items): {names}")
                elif batch_kind == "multidisc" and len(existing) > 1:
                    names = " + ".join(p.name for p in existing)
                    self._log(f"Processing multi-disc bundle ({len(existing)} items): {names}")
                else:
                    self._log(f"Processing job: {existing[0].name}")
                outcome = execute_ingestion_batch(batch_kind, existing, self.config, dry_run=False)
                with self._lock:
                    self._retry_counts.pop(batch, None)
                    self._retry_schedule.pop(batch, None)
                self._on_outcome(outcome)
            except (RoutingError, InsufficientStagingSpaceError, OSError) as exc:
                try:
                    self._on_error(primary, exc)
                except Exception:
                    pass
                self._schedule_transient_retry(batch, primary)
            except (ArchiveError, ConversionError, Exception) as exc:
                with self._lock:
                    self._retry_counts.pop(batch, None)
                    self._retry_schedule.pop(batch, None)
                try:
                    self._on_error(primary, exc)
                except Exception:
                    pass
            finally:
                with self._lock:
                    for p in batch:
                        self._queued_or_active.discard(p)
                self.job_queue.task_done()

    def start_background(self, scan_existing: bool = True) -> None:
        """Start Watchdog observer, stability poller, and sequential worker threads."""
        ensure_local_directories(self.config)
        purge_stale_staging(self.config)
        self._watcher_lock = acquire_staging_lock(self.config.paths.staging_dir / ".lock_watcher")

        if scan_existing:
            for existing in discover_watch_candidates(self.config.paths.watch_dir, config=self.config):
                self.register_candidate(existing)

        handler = _WatchdogDropHandler(
            self.register_candidate,
            self.config.paths.watch_dir,
            platform_buckets=self._platform_buckets,
        )
        observer = Observer()
        observer.schedule(handler, str(self.config.paths.watch_dir), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer

        self._poller_thread = threading.Thread(
            target=self._stability_poll_loop,
            name="volundr-stability-poller",
            daemon=True,
        )
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="volundr-sequential-worker",
            daemon=False,
        )
        self._poller_thread.start()
        self._worker_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Gracefully stop observer, poller, and sequential worker (terminating hung subprocesses on shutdown)."""
        self._stop_event.set()
        try:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join(timeout=5.0)
                self._observer = None
            self.job_queue.put(None)
            if self._poller_thread is not None:
                self._poller_thread.join(timeout=5.0)
            if self._worker_thread is not None:
                grace = min(timeout, 5.0)
                self._worker_thread.join(timeout=grace)
                if self._worker_thread.is_alive():
                    terminate_active_subprocesses(grace_seconds=2.0)
                    remaining = max(1.0, timeout - grace)
                    self._worker_thread.join(timeout=remaining)
        finally:
            if self._watcher_lock is not None:
                self._watcher_lock.release()
                self._watcher_lock = None


def wait_until_target_stable(target: Path, config: PipelineConfig, max_wait_seconds: float = 60.0) -> bool:
    """Wait synchronously for `target` to pass stability & exclusive-lock checks (used by `scan`)."""
    poll_sec = config.general.stability_poll_seconds
    needed = config.general.stability_checks_required
    state = _StabilityState()
    elapsed = 0.0
    stop_evt = threading.Event()

    while elapsed <= max_wait_seconds:
        ready, sig = probe_target_snapshot(target)
        if ready:
            if state.last_signature == sig:
                state.stable_hits += 1
            else:
                state.last_signature = sig
                state.stable_hits = 1
            if state.stable_hits >= needed:
                return True
        else:
            state.last_signature = None
            state.stable_hits = 0

        if needed <= 1 and ready:
            return True
        stop_evt.wait(poll_sec)
        elapsed += poll_sec

    return False


def scan_watch_dir_once(
    config: PipelineConfig,
    dry_run: bool = False,
    verify_stability: bool = True,
    on_outcome: Callable[[RoutingOutcome], None] | None = None,
    on_error: Callable[[Path, Exception], None] | None = None,
) -> tuple[list[RoutingOutcome], int]:
    """
    One-shot sequential backlog pass over `watch_dir` (`python main.py scan`).

    Returns `(outcomes, error_count)`.
    """
    ensure_local_directories(config)
    if not dry_run:
        purge_stale_staging(config)

    candidates = discover_watch_candidates(config.paths.watch_dir, config=config)
    stable_candidates: list[Path] = []

    for cand in candidates:
        if verify_stability and not dry_run:
            if not wait_until_target_stable(cand, config):
                if on_error:
                    on_error(
                        cand,
                        RuntimeError("File/folder remained locked or unstable; skipped for safety"),
                    )
                continue
        stable_candidates.append(cand)

    batches = group_ingestion_batches(stable_candidates, config)
    seq_queue: queue.Queue[tuple[str, list[Path]]] = queue.Queue()
    for b_kind, batch in batches:
        seq_queue.put((b_kind, batch))

    outcomes: list[RoutingOutcome] = []
    errors = 0
    while not seq_queue.empty():
        b_kind, batch = seq_queue.get()
        try:
            out = execute_ingestion_batch(b_kind, batch, config, dry_run=dry_run)
            outcomes.append(out)
            if on_outcome:
                on_outcome(out)
        except (ArchiveError, ConversionError, RoutingError, Exception) as exc:
            errors += 1
            if on_error:
                on_error(batch[0], exc)
        finally:
            seq_queue.task_done()

    return outcomes, errors
