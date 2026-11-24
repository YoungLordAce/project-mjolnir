"""Nintendo Switch TitleID family math, cross-archive sibling bundling, and NAS donor lookup."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import functools
import os
from pathlib import Path
import re
import zipfile

from src.archive_handler import ArchiveError, is_archive, list_archive_entries
from src.constants import (
    IGNORED_FILENAMES as _IGNORED_FILENAMES,
    NON_ROM_SIDECAR_EXTENSIONS as _SWITCH_IGNORED_SIDECARS,
    is_syncthing_temp,
)
from src.models import PipelineConfig

SWITCH_PACKAGE_EXTENSIONS: frozenset[str] = frozenset({".nsp", ".xci", ".nsz", ".xcz"})

# Horizon OS 64-bit Application / Patch / AOC TitleID masks
SWITCH_FAMILY_MASK = 0xFFFFFFFFFFFFE000
SWITCH_OFFSET_MASK = 0x0000000000001FFF

_BRACKET_TID_RE = re.compile(r"\[\s*(01[0-9A-Fa-f]{14})\s*\]")
_ANY_TID_RE = re.compile(r"(?<![0-9A-Fa-f])(01[0-9A-Fa-f]{14})(?![0-9A-Fa-f])")
_TIK_FILENAME_RE = re.compile(r"^(01[0-9A-Fa-f]{14})[0-9A-Fa-f]{16}\.tik$", re.IGNORECASE)
_PFS0_XML_TID_RE = re.compile(rb"(?i)<Id>\s*(?:0x)?(01[0-9A-Fa-f]{14})\s*</Id>")
_VERSION_TAG_RE = re.compile(r"[\[\(]\s*v(\d+)\s*[\]\)]", re.IGNORECASE)

# NSC_Builder content summary tag, e.g. `(1G+1U+5D)`, `(5D)`, `(1U+2D)`
_NSCB_CONTENT_TAG_RE = re.compile(
    r"\(\s*(?:(\d+)G)?(?:\s*\+?\s*(\d+)U)?(?:\s*\+?\s*(\d+)D)?\s*\)",
    re.IGNORECASE,
)
_ADDON_KEYWORD_RE = re.compile(
    r"(?:\[|\(|(?<![A-Za-z0-9]))(?:\d*\s*DLCs?|UPD(?:ATE)?|PATCH)(?:\s*[-–—_]|\s*\]|\s*\)|(?![A-Za-z0-9]))",
    re.IGNORECASE,
)
_UPDATE_KEYWORD_RE = re.compile(
    r"(?:\[|\(|(?<![A-Za-z0-9]))(?:UPD(?:ATE)?|PATCH)(?:\s*[-–—_]|\s*\]|\s*\)|(?![A-Za-z0-9]))",
    re.IGNORECASE,
)


def _normalize_switch_title_key(raw_text: str) -> str:
    """
    Extract a normalized base-game title key for cross-matching untagged Switch archives
    (e.g. `'Sea of Stars (USA, Europe) (Rev 1)'` and `'Sea_of_Stars_Update_v2.0.59001'` -> `'sea of stars'`).
    """
    text = raw_text.replace("_", " ")
    # Strip bracketed/parenthesized metadata tags (`(USA, Europe)`, `[0100...]`, `[Merged]`, etc.)
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
    # Strip trailing update/DLC/version/scene-group tokens
    text = re.sub(
        r"\b(?:update|upd|patch|dlc|dlcs|v\d+(?:\.\d+)*|nsw|venom|hr|duplex|bigbluebox|suplex|merged)\b.*$",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    # If title has a `- DLC Subtitle` suffix, keep the main title prefix before ` - `
    if " - " in text:
        prefix = text.split(" - ", 1)[0].strip()
        if len(prefix) >= 3:
            text = prefix
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s{2,}", " ", text).strip()


class SwitchContentRole(StrEnum):
    """Role of a Switch content package within its Horizon OS TitleID family."""

    BASE = "base"      # offset == 0x0000
    UPDATE = "update"  # offset == 0x0800
    DLC = "dlc"        # offset in 0x0001..0x1FFF (excluding 0x0800)


@dataclass(frozen=True, slots=True)
class SwitchSourceInfo:
    """Lightweight pre-extraction metadata for a Switch ROM file, archive, or folder."""

    path: Path
    family_ids: tuple[str, ...]
    roles: frozenset[SwitchContentRole]
    title_ids: tuple[str, ...]
    package_count: int

    @property
    def primary_family_id(self) -> str | None:
        """Return the shared 16-hex Base Family ID if unambiguous."""
        return self.family_ids[0] if len(self.family_ids) == 1 else None

    @property
    def has_base(self) -> bool:
        """True if this source contains a Base game (`0x0000` offset)."""
        return SwitchContentRole.BASE in self.roles

    @property
    def is_addon_only(self) -> bool:
        """True if TitleIDs are known and contain ONLY Update(s) and/or DLC(s) (no Base game)."""
        return bool(self.family_ids) and not self.has_base


def classify_switch_title_id(raw_tid: str) -> tuple[str, SwitchContentRole]:
    """
    Classify a 16-character hex Switch TitleID into `(family_base_id, role)`.

    Horizon OS allocates each game an `0x2000` (8192-ID) block aligned to `0xFFFFFFFFFFFFE000`:
      - `tid & 0x1FFF == 0x0000` -> `BASE`   (e.g. `01002E7016C46000`)
      - `tid & 0x1FFF == 0x0800` -> `UPDATE` (e.g. `01002E7016C46800` -> `01002E7016C46000`)
      - any other offset         -> `DLC`    (e.g. `01002E7016C47001..7005` -> `01002E7016C46000`)
    """
    cleaned = raw_tid.strip().upper()
    if len(cleaned) != 16 or not cleaned.startswith("01"):
        raise ValueError(f"Invalid Switch TitleID: {raw_tid!r}")
    tid_int = int(cleaned, 16)
    family_id = f"{tid_int & SWITCH_FAMILY_MASK:016X}"
    offset = tid_int & SWITCH_OFFSET_MASK
    if offset == 0x0000:
        return family_id, SwitchContentRole.BASE
    if offset == 0x0800:
        return family_id, SwitchContentRole.UPDATE
    return family_id, SwitchContentRole.DLC


def has_explicit_addon_only_tag(filename: str) -> bool:
    """
    Return True if `filename` carries an NSC_Builder tag like `(5D)` or `(1U+2D)` with 0 `G`s,
    or explicit add-on keywords like `[DLC]`, `[3 DLCs]`, `[DLC - ...]`, `[Update]`, `[Patch]`,
    indicating an Update/DLC-only package/container lacking a Base game.
    """
    if _ADDON_KEYWORD_RE.search(filename):
        return True
    for match in _NSCB_CONTENT_TAG_RE.finditer(filename):
        g_str, u_str, d_str = match.group(1), match.group(2), match.group(3)
        if not g_str and not u_str and not d_str:
            continue
        g_count = int(g_str) if g_str else 0
        u_count = int(u_str) if u_str else 0
        d_count = int(d_str) if d_str else 0
        if g_count == 0 and (u_count > 0 or d_count > 0):
            return True
    return False


def extract_title_ids_from_name(text: str) -> tuple[str, ...]:
    """Extract normalized 16-hex Switch TitleIDs (`01..............`) from a filename."""
    found: list[str] = []
    for m in _BRACKET_TID_RE.finditer(text):
        tid = m.group(1).upper()
        if tid not in found:
            found.append(tid)
    if found:
        return tuple(found)

    for m in _ANY_TID_RE.finditer(text):
        tid = m.group(1).upper()
        if tid not in found:
            found.append(tid)
    return tuple(found)


def extract_title_ids_from_pfs0_bytes(header: bytes) -> tuple[str, ...]:
    """
    Extract TitleIDs from a `.nsp`/`.nsz` `PFS0` header by inspecting `<rights_id>.tik` string table
    entries and embedded `cnmt.xml` `<Id>` tags (pure Python, zero external subprocess calls).
    """
    if len(header) < 16 or header[:4] != b"PFS0":
        return ()

    num_files = int.from_bytes(header[4:8], "little")
    str_table_size = int.from_bytes(header[8:12], "little")
    if num_files <= 0 or num_files > 4096 or str_table_size <= 0 or str_table_size > 262144:
        return ()

    str_offset = 16 + (num_files * 24)
    found: list[str] = []

    # 1. Inspect PFS0 string table for `<rights_id>.tik` entries
    if str_offset < len(header):
        raw_table = header[str_offset : str_offset + str_table_size]
        for raw_entry in raw_table.split(b"\x00"):
            if not raw_entry:
                continue
            entry_name = raw_entry.decode("ascii", errors="ignore").strip()
            tik_match = _TIK_FILENAME_RE.match(entry_name)
            if tik_match:
                tid = tik_match.group(1).upper()
                if tid not in found:
                    found.append(tid)

    # 2. Inspect embedded `cnmt.xml` payload for `<Id>0x...</Id>` tags (common in ticketless NSPs)
    for m in _PFS0_XML_TID_RE.finditer(header):
        tid = m.group(1).decode("ascii", errors="ignore").upper()
        if tid.startswith("01") and tid not in found:
            found.append(tid)

    return tuple(found)


@functools.lru_cache(maxsize=4096)
def _extract_switch_tids_cached(
    resolved_path: Path,
    mtime_ns: int,
    size: int,
) -> tuple[str, ...]:
    """Bounded cache (`maxsize=4096`) for Switch TitleID extraction keyed on `(path, mtime_ns, size)`."""
    if resolved_path.suffix.lower() in (".nsp", ".nsz") and size > 0:
        try:
            with resolved_path.open("rb") as fh:
                head = fh.read(131072)
            pfs0_tids = extract_title_ids_from_pfs0_bytes(head)
            if pfs0_tids:
                return pfs0_tids
        except OSError:
            pass

    by_name = extract_title_ids_from_name(resolved_path.name)
    if by_name:
        return by_name

    return ()


def extract_switch_title_ids_from_file(file_path: Path) -> tuple[str, ...]:
    """Extract Switch TitleIDs from `file_path`'s `PFS0` `.tik`/`cnmt.xml` header or filename."""
    try:
        st = file_path.stat()
        if file_path.is_file():
            return _extract_switch_tids_cached(file_path.resolve(), st.st_mtime_ns, st.st_size)
    except OSError:
        pass

    by_name = extract_title_ids_from_name(file_path.name)
    if by_name:
        return by_name

    return ()


def parse_switch_version(name: str) -> int:
    """Extract numeric `[v1966080]` or `(v524288)` version tag from filename (0 if absent)."""
    m = _VERSION_TAG_RE.search(name)
    return int(m.group(1)) if m else 0


def extract_switch_version_from_source(path: Path, config: PipelineConfig) -> int:
    """
    Extract the highest numeric Switch version tag (`[v...]` / `(v...)`) from a Switch package,
    archive (peeking its entry list via cache), or directory. Returns 0 if no version tag is found.
    """
    direct = parse_switch_version(path.name)
    if direct > 0:
        return direct

    if path.is_file() and is_archive(path):
        try:
            entries = list_archive_entries(path, config.binaries.seven_zip)
        except ArchiveError:
            return 0
        versions = [
            parse_switch_version(e.filename)
            for e in entries
            if not e.is_dir and e.suffix in SWITCH_PACKAGE_EXTENSIONS
        ]
        return max(versions, default=0)

    if path.is_dir():
        try:
            versions = [
                parse_switch_version(p.name)
                for p in path.rglob("*")
                if p.is_file() and p.suffix.lower() in SWITCH_PACKAGE_EXTENSIONS
            ]
        except OSError:
            return 0
        return max(versions, default=0)

    return 0


def is_redundant_switch_update(
    incoming_paths: list[Path],
    nas_donor: Path,
    config: PipelineConfig,
) -> tuple[bool, int, int]:
    """
    Return `(is_redundant, max_incoming_ver, nas_ver)` for an Update-only Switch batch.

    Conservative by design: only returns `True` when BOTH `nas_donor` carries a positive numeric
    version (`> 0`) AND every incoming update carries a positive numeric version with
    `max(incoming_versions) <= nas_ver`.
    """
    nas_ver = parse_switch_version(nas_donor.name)
    if nas_ver <= 0 or not incoming_paths:
        return False, 0, nas_ver

    incoming_vers = [extract_switch_version_from_source(p, config) for p in incoming_paths]
    if not incoming_vers or any(v <= 0 for v in incoming_vers):
        return False, max(incoming_vers, default=0), nas_ver

    max_inc = max(incoming_vers)
    return (max_inc <= nas_ver), max_inc, nas_ver


def switch_package_sort_key(file_path: Path) -> tuple[int, int, int, str]:
    """
    Deterministic sort key for `mlist.txt` so `BASE` always precedes `UPDATE`, which precedes `DLC`.
    """
    tids = extract_switch_title_ids_from_file(file_path)
    role_priority = {
        SwitchContentRole.BASE: 0,
        SwitchContentRole.UPDATE: 1,
        SwitchContentRole.DLC: 2,
    }
    is_addon_tagged = has_explicit_addon_only_tag(file_path.name)
    if tids:
        _, role = classify_switch_title_id(tids[0])
        if role == SwitchContentRole.BASE and is_addon_tagged:
            role = (
                SwitchContentRole.UPDATE
                if _UPDATE_KEYWORD_RE.search(file_path.name)
                else SwitchContentRole.DLC
            )
        rank = role_priority[role]
        tid_int = int(tids[0], 16)
    else:
        if is_addon_tagged:
            role = (
                SwitchContentRole.UPDATE
                if _UPDATE_KEYWORD_RE.search(file_path.name)
                else SwitchContentRole.DLC
            )
        else:
            role = SwitchContentRole.BASE
        rank = role_priority[role]
        tid_int = 0

    return (rank, tid_int, parse_switch_version(file_path.name), file_path.name.lower())


def _summarize_tids(
    tids: list[str],
    names_for_tag_check: list[str],
    path: Path,
    pkg_count: int,
) -> SwitchSourceInfo:
    families: list[str] = []
    roles: set[SwitchContentRole] = set()
    unique_tids: list[str] = []
    addon_only_override = (
        has_explicit_addon_only_tag(path.name)
        or (
            bool(names_for_tag_check)
            and all(has_explicit_addon_only_tag(n) for n in names_for_tag_check if n)
        )
    )

    for tid in tids:
        if tid not in unique_tids:
            unique_tids.append(tid)
        fam, role = classify_switch_title_id(tid)
        if role == SwitchContentRole.BASE and addon_only_override:
            role = (
                SwitchContentRole.UPDATE
                if _UPDATE_KEYWORD_RE.search(" ".join([path.name, *names_for_tag_check]))
                else SwitchContentRole.DLC
            )
        if fam not in families:
            families.append(fam)
        roles.add(role)

    # When a Switch package or archive (e.g. No-Intro `.xci` inside `.7z`) carries no TitleID in
    # its filename/PFS0 header, infer its role from whether it carries an explicit add-on tag.
    if not roles:
        if addon_only_override:
            combined_names = " ".join([path.name, *names_for_tag_check])
            if _UPDATE_KEYWORD_RE.search(combined_names):
                roles.add(SwitchContentRole.UPDATE)
            else:
                roles.add(SwitchContentRole.DLC)
        else:
            roles.add(SwitchContentRole.BASE)

    return SwitchSourceInfo(
        path=path,
        family_ids=tuple(families),
        roles=frozenset(roles),
        title_ids=tuple(unique_tids),
        package_count=pkg_count,
    )


@functools.lru_cache(maxsize=4096)
def _probe_switch_file_cached(
    resolved_path: Path,
    mtime_ns: int,
    size: int,
    seven_zip_bin: Path | None,
) -> SwitchSourceInfo | None:
    """
    Bounded cache (`maxsize=4096`) for probing a single Switch package or archive,
    keyed on `(resolved_path, mtime_ns, size, seven_zip_bin)`.
    """
    ext = resolved_path.suffix.lower()

    # 1. Loose Switch package (`.nsp`, `.xci`, `.nsz`, `.xcz`)
    if ext in SWITCH_PACKAGE_EXTENSIONS:
        tids = list(_extract_switch_tids_cached(resolved_path, mtime_ns, size))
        return _summarize_tids(tids, [resolved_path.name], resolved_path, pkg_count=1)

    # 2. Archive (`.zip`, `.7z`, `.rar`)
    if is_archive(resolved_path):
        try:
            entries = list_archive_entries(resolved_path, seven_zip_bin)
        except ArchiveError:
            return None

        actionable = [
            e for e in entries
            if not e.is_dir
            and e.filename.lower() not in _IGNORED_FILENAMES
            and e.suffix not in _SWITCH_IGNORED_SIDECARS
        ]
        if not actionable:
            return None
        if not all(e.suffix in SWITCH_PACKAGE_EXTENSIONS for e in actionable):
            return None

        tids = []
        entry_names = [e.filename for e in actionable]
        for e in actionable:
            for tid in extract_title_ids_from_name(e.filename):
                tids.append(tid)

        if not tids:
            tids.extend(extract_title_ids_from_name(resolved_path.name))

        # Fast stdlib zip header peek to discover real internal TitleIDs
        if ext == ".zip":
            try:
                with zipfile.ZipFile(resolved_path, "r") as zf:
                    pfs0_tids: list[str] = []
                    for e in actionable:
                        if e.suffix in (".nsp", ".nsz"):
                            with zf.open(e.path) as zef:
                                pfs0_tids.extend(extract_title_ids_from_pfs0_bytes(zef.read(131072)))
                    if pfs0_tids:
                        tids = pfs0_tids
            except (zipfile.BadZipFile, KeyError, OSError, NotImplementedError):
                pass

        return _summarize_tids(tids, entry_names, resolved_path, pkg_count=len(actionable))

    return None


def probe_switch_source(path: Path, config: PipelineConfig) -> SwitchSourceInfo | None:
    """
    Lightweight pre-extraction probe: if `path` is a Switch package, an archive containing only
    Switch packages (+ ignorable sidecars), or a folder of Switch packages/archives, return its
    `SwitchSourceInfo`; otherwise return `None`.
    """
    if not path.exists():
        return None

    # 1 & 2. Loose Switch package or Archive (cached by `(resolved_path, mtime_ns, size)`)
    if path.is_file():
        ext = path.suffix.lower()
        if ext not in SWITCH_PACKAGE_EXTENSIONS and not is_archive(path):
            return None
        try:
            st = path.stat()
            resolved = path.resolve()
        except OSError:
            return None
        info = _probe_switch_file_cached(
            resolved,
            st.st_mtime_ns,
            st.st_size,
            config.binaries.seven_zip,
        )
        if info is not None and info.path != path:
            return replace(info, path=path)
        return info

    # 3. Directory (loose Switch files and/or nested Switch archives)
    if path.is_dir():
        try:
            children = sorted(p for p in path.rglob("*") if p.is_file())
        except OSError:
            return None

        sub_infos: list[SwitchSourceInfo] = []
        for child in children:
            if child.name.lower() in _IGNORED_FILENAMES:
                continue
            ext = child.suffix.lower()
            if ext in _SWITCH_IGNORED_SIDECARS:
                continue
            if ext in SWITCH_PACKAGE_EXTENSIONS or is_archive(child):
                info = probe_switch_source(child, config)
                if info is None:
                    return None
                sub_infos.append(info)
            else:
                return None

        if not sub_infos:
            return None

        all_fams: list[str] = []
        all_roles: set[SwitchContentRole] = set()
        all_tids: list[str] = []
        total_pkgs = 0
        for si in sub_infos:
            for f in si.family_ids:
                if f not in all_fams:
                    all_fams.append(f)
            all_roles.update(si.roles)
            for t in si.title_ids:
                if t not in all_tids:
                    all_tids.append(t)
            total_pkgs += si.package_count

        return SwitchSourceInfo(
            path=path,
            family_ids=tuple(all_fams),
            roles=frozenset(all_roles),
            title_ids=tuple(all_tids),
            package_count=total_pkgs,
        )

    return None


def _source_group_sort_key(info: SwitchSourceInfo) -> tuple[int, str]:
    """Order sources within a Switch family group so BASE arrives first, then UPDATE, then DLC."""
    if SwitchContentRole.BASE in info.roles:
        rank = 0
    elif SwitchContentRole.UPDATE in info.roles:
        rank = 1
    elif SwitchContentRole.DLC in info.roles:
        rank = 2
    else:
        rank = 3
    return (rank, info.path.name.lower())


def group_candidates_with_switch_families(
    candidates: list[Path],
    config: PipelineConfig,
) -> list[list[Path]]:
    """
    Partition top-level candidates into execution batches, coalescing separate Switch archives,
    loose `.nsp`/`.xci` files, or folders that share the same Horizon OS `primary_family_id`.
    """
    from src.identifier import sanitize_rom_stem

    probed: dict[Path, SwitchSourceInfo | None] = {}
    family_buckets: dict[str, list[SwitchSourceInfo]] = {}

    for cand in candidates:
        resolved = cand.expanduser().resolve()
        info = probe_switch_source(resolved, config)
        probed[cand] = info
        if info is not None and info.primary_family_id is not None:
            family_buckets.setdefault(info.primary_family_id, []).append(info)

    # Secondary title-stem association: if a candidate is a valid Switch archive/file that lacked TitleIDs,
    # match it with an existing family bucket whose members share the same clean game stem!
    for cand, info in list(probed.items()):
        if info is None or info.primary_family_id is not None:
            continue
        raw_cand_name = cand.name if cand.is_dir() else cand.stem
        cand_stem = sanitize_rom_stem(raw_cand_name).lower()
        cand_key = _normalize_switch_title_key(raw_cand_name)
        if not cand_stem and not cand_key:
            continue
        for fam_id, bucket_members in family_buckets.items():
            matched = False
            for m in bucket_members:
                m_raw = m.path.name if m.path.is_dir() else m.path.stem
                if sanitize_rom_stem(m_raw).lower() == cand_stem:
                    matched = True
                    break
                if cand_key and _normalize_switch_title_key(m_raw) == cand_key:
                    matched = True
                    break
            if matched:
                augmented = SwitchSourceInfo(
                    path=info.path,
                    family_ids=(fam_id,),
                    roles=info.roles or frozenset({SwitchContentRole.BASE}),
                    title_ids=info.title_ids or (fam_id,),
                    package_count=info.package_count,
                )
                probed[cand] = augmented
                family_buckets[fam_id].append(augmented)
                break

    emitted_families: set[str] = set()
    batches: list[list[Path]] = []

    for cand in candidates:
        info = probed[cand]
        fam = info.primary_family_id if info is not None else None
        if fam is None:
            batches.append([cand])
            continue
        if fam in emitted_families:
            continue
        emitted_families.add(fam)
        sorted_members = sorted(family_buckets[fam], key=_source_group_sort_key)
        batches.append([m.path for m in sorted_members])

    return batches


@functools.lru_cache(maxsize=64)
def _find_nas_switch_family_files_cached(
    switch_dir: Path,
    dir_signature: tuple[tuple[str, int, int], ...],
    family_id: str,
    clean_hint: str | None,
    seven_zip_bin: Path | None,
) -> tuple[Path | None, tuple[Path, ...]]:
    """
    Bounded cache (`maxsize=64`) for NAS Switch family lookup keyed on directory entry
    `(name, mtime_ns, size)` tuples so 24/7 watch polling never re-opens unchanged NAS files.
    """
    from src.identifier import sanitize_rom_stem

    matching: list[Path] = []
    base_donors: list[tuple[Path, int]] = []

    norm_hint = _normalize_switch_title_key(clean_hint) if clean_hint else ""
    for fname, mtime_ns, size in dir_signature:
        entry = switch_dir / fname
        info = _probe_switch_file_cached(entry.resolve(), mtime_ns, size, seven_zip_bin)
        is_fam_match = info is not None and family_id in info.family_ids
        entry_clean = sanitize_rom_stem(entry.stem).lower()
        is_title_match = clean_hint is not None and (
            entry_clean == clean_hint
            or (bool(norm_hint) and _normalize_switch_title_key(entry.stem) == norm_hint)
        )

        if not is_fam_match and not is_title_match:
            continue

        matching.append(entry)
        has_base_role = info.has_base if info is not None else (entry.suffix.lower() in (".xci", ".nsp"))
        if has_base_role:
            base_donors.append((entry, size))

    if not base_donors:
        return None, tuple(matching)

    # Prefer `.xci` (already merged container) -> highest `[v...]` -> largest file size
    base_donors.sort(
        key=lambda item: (
            0 if item[0].suffix.lower() == ".xci" else 1,
            -parse_switch_version(item[0].name),
            -item[1],
        )
    )
    return base_donors[0][0], tuple(matching)


def find_nas_switch_family_files(
    switch_dir: Path,
    family_id: str,
    config: PipelineConfig,
    game_title_hint: str | None = None,
) -> tuple[Path | None, list[Path]]:
    """
    Search top-level `switch_dir` on the NAS for existing files belonging to `family_id`.

    Returns `(best_base_donor_or_none, all_matching_family_files)`.
    Prefers an existing `.xci` containing `BASE` (so previously merged DLCs/updates carry forward
    when applying a newer update or extra DLC), falling back to a Base `.nsp`/`.nsz`/`.xcz`.
    """
    from src.identifier import sanitize_rom_stem

    if not switch_dir.is_dir():
        return None, []

    file_tuples: list[tuple[str, int, int]] = []
    try:
        with os.scandir(switch_dir) as it:
            for de in it:
                if is_syncthing_temp(de.name):
                    continue
                dot = de.name.rfind(".")
                ext = de.name[dot:].lower() if dot > 0 else ""
                if ext not in SWITCH_PACKAGE_EXTENSIONS:
                    continue
                try:
                    if not de.is_file():
                        continue
                    st = de.stat()
                except OSError:
                    continue
                file_tuples.append((de.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return None, []

    dir_signature = tuple(sorted(file_tuples, key=lambda item: item[0].lower()))
    clean_hint = (
        sanitize_rom_stem(game_title_hint).lower()
        if game_title_hint is not None
        else None
    )
    donor, matching_tuple = _find_nas_switch_family_files_cached(
        switch_dir,
        dir_signature,
        family_id,
        clean_hint,
        config.binaries.seven_zip,
    )
    return donor, list(matching_tuple)
