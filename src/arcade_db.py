"""Arcade detection heuristics and 5-way FBNeo sub-platform classification.

Routes MAME / FinalBurn Neo ROM archives into Cocoon FE's five arcade folders:
  - `cps1`   (Capcom Play System I & CPS-1.5 QSound)
  - `cps2`   (Capcom Play System II)
  - `cps3`   (Capcom Play System III)
  - `neogeo` (SNK Neo Geo MVS / AES)
  - `fbneo`  (All other arcade hardware supported by FinalBurn Neo)
"""

from __future__ import annotations

import re
from src.constants import ARCADE_IGNORABLE_SIDECAR_EXTENSIONS as IGNORABLE_SIDECAR_EXTENSIONS
from src.models import ArchiveEntry, DetectionMethod

# Any archive containing even ONE of these console/disc/document extensions is NOT an arcade set.
NON_ARCADE_EXTENSIONS: frozenset[str] = frozenset({
    # Disc images & descriptors
    ".iso", ".cue", ".gdi", ".ccd", ".mds", ".mdf", ".chd", ".rvz",
    ".wbfs", ".gcm", ".gcz", ".ciso", ".m3u", ".nrg", ".ecm", ".cdi",
    # Nintendo handhelds & consoles
    ".nes", ".fds", ".unf", ".unif", ".sfc", ".smc", ".fig", ".swc",
    ".gb", ".gbc", ".gba", ".agb", ".nds", ".dsi", ".ids",
    ".3ds", ".cci", ".zcci", ".cia", ".3dsx",
    ".n64", ".z64", ".v64", ".ndd",
    ".wad", ".wux", ".wua", ".wud", ".wuhb", ".rpx",
    ".app", ".h3", ".tmd", ".tik", ".cert",
    ".nsp", ".xci", ".nsz", ".xcz",
    # Sony handhelds & consoles
    ".cso", ".pbp", ".pkg", ".vpk", ".dpt", ".sfo", ".suprx", ".skprx", ".self", ".psarc",
    # Sega / NEC / Other consoles
    ".md", ".smd", ".gen", ".32x", ".sms", ".gg",
    ".pce", ".sgx", ".ws", ".wsc", ".ngp", ".ngc",
    ".a26", ".a52", ".a78", ".lnx", ".j64", ".col", ".int",
    # Common PC / non-ROM archive payloads
    ".exe", ".msi", ".dll", ".bat", ".sh", ".apk", ".ipa",
    ".pdf", ".epub", ".mp3", ".flac", ".ogg", ".wav", ".mp4", ".mkv", ".avi",
    ".zip", ".7z", ".rar",
})

# Explicit arcade chip extensions & board-socket suffixes (e.g. .rom, .ic12, .7f, .p1, .q01)
ARCADE_CHIP_EXPLICIT_EXTS: frozenset[str] = frozenset({
    ".rom", ".bin", ".pal", ".chr", ".prg", ".key", ".nv", ".nvram", ".eeprom",
    ".snd", ".cpu", ".gfx", ".sub", ".main", ".bios", ".sfix", ".lo", ".sp1",
    ".p1", ".p2", ".p3", ".c1", ".c2", ".c3", ".c4", ".c5", ".c6", ".c7", ".c8",
    ".s1", ".m1", ".v1", ".v2", ".v3", ".v4",
})

# Matches IC/U socket suffixes (.ic1, .u24), board coordinates (.7f, .10d, .1a), or split banks (.01, .q02, .m01)
_CHIP_EXT_RE = re.compile(
    r"^\.(?:ic\d+[a-z]?|u\d+[a-z]?|[0-9]{1,3}[a-z]{0,2}|[a-z][0-9]{1,3}[a-z]?)$",
    re.IGNORECASE,
)

# Matches classic EPROM/mask-ROM filenames even when extensionless (e.g. epr-12345, mpr-15398, 201-p1, 10, sf2_23a)
_CHIP_NAME_RE = re.compile(
    r"^(?:(?:epr|mpr|opr|fpr|gpr|tpr)[-_]?\d+|[0-9]{2,3}|[0-9a-z]{2,8}[-_][0-9a-z]{1,5})$",
    re.IGNORECASE,
)

# SNK Neo Geo chip nomenclature: e.g. `201-p1.p1`, `265-c1.bin`, `052-s1.s1`, `096-m1.m1`, `242-v1.v1`
_NEOGEO_CHIP_RE = re.compile(
    r"^(?:[0-9]{3}|ng[0-9a-z]+)-[pcsmv][1-9]",
    re.IGNORECASE,
)
_NEOGEO_BIOS_FILES: frozenset[str] = frozenset({
    "000-lo.lo", "sfix.sfix", "sm1.sm1", "sp-s2.sp1", "sp-s.sp1", "sp-u2.sp1", "uni-bios.rom",
})

# CPS3 SIMM bank filenames inside CPS3 zips
_CPS3_SIMM_NAMES: frozenset[str] = frozenset({
    "10", "11", "20", "21", "30", "31", "40", "41", "50", "51", "60", "61",
})

# CPS2 QSound + encrypted program split naming
_CPS2_PROG_RE = re.compile(r"\.(?:0[3-9][a-z]?|10[a-z]?)$", re.IGNORECASE)
_CPS2_QSOUND_RE = re.compile(r"\.(?:q0[1-9][a-z]?|m0[1-9][a-z]?)$", re.IGNORECASE)

# ------------------------------------------------------------------------------
# Curated FBNeo / MAME Shortname Prefix Families for CPS3, CPS2, CPS1, Neo Geo
# ------------------------------------------------------------------------------
CPS3_PREFIXES: tuple[str, ...] = (
    "cps3boot", "sfiii", "jojo", "redearth", "warzard",
)

CPS2_EXACT_OR_PREFIXES: tuple[str, ...] = (
    "qsound", "ssf2", "xmcota", "msh", "nwarr", "vhunt", "vsav", "cybots",
    "sfa", "sfz", "xmvsf", "mshvsf", "mvsc", "dstlk", "ringdest", "armwar",
    "avsp", "ddtod", "ddsom", "ecofghtr", "gigawing", "mmatrix", "mpang",
    "progear", "pzloop2", "spf2", "sgemf", "pfghtj", "csclub", "dimahoo",
    "gmahou", "19xx", "1944", "batcir", "choko", "jyangoku", "hsf2",
    "megaman2", "megamn2", "rockman2", "rmancp2", "puzzloop2",
)

CPS1_EXACT_OR_PREFIXES: tuple[str, ...] = (
    "forgottn", "lostwrld", "ghouls", "daimakai", "strider", "dynwar",
    "tenchi", "willow", "unsquad", "area88", "ffight", "1941", "mercs",
    "senjo", "mtwins", "chikij", "msword", "cawing", "usnavy", "nemo",
    "sf2", "3wonders", "wonder3", "kod", "captcomm", "knights", "varth",
    "cworld2j", "qad", "qtono2", "wof", "dino", "cadillac", "punisher",
    "slammast", "mbomber", "mbombrd", "pnickj", "pang3", "megaman",
    "rockmanj", "ganbare", "pokonyan", "sfzch", "fcrash", "kodb",
)
# Prevent CPS2 `sfz`, `sfz2`, `sfz3` or `megaman2` from colliding with CPS1 `sfzch` / `megaman`:
# Checked in strict order: CPS3 -> CPS2 -> CPS1 -> NeoGeo!

NEOGEO_PREFIXES: tuple[str, ...] = (
    "neogeo", "mslug", "kof9", "kof2", "kf2k", "samsho", "samurai", "fatfur",
    "rbff", "garou", "aof", "artoff", "lastblad", "lastbld", "shocktr",
    "blazstar", "pulstar", "viewpoin", "windjamm", "turfmast", "twinspri",
    "spinmast", "nam1975", "maglord", "cyberlip", "sengoku", "wh1", "wh2",
    "whp", "svc", "matrim", "rotd", "nitd", "sonicwi2", "sonicwi3", "aerofig2",
    "zedblade", "wakuwak7", "kabukikl", "Karnovr", "karnovr", "breakers",
    "breakrev", "ninjamas", "ragnagrd", "overtop", "neodrift", "neobombe",
    "neomrdo", "panicbom", "pbobblen", "pbobbl2n", "magicaldrop", "magdrop2",
    "magdrop3", "moneyidp", "ghostlop", "tophuntr", "mutnat", "roboarmy",
    "eightman", "burningf", "crsword", "thrashr", "alpham2", "aso2",
    "androdun", "lresort", "preisle2", "s1945p", "zupapa", "bangbead",
    "flipshot", "ganryu", "goalx3", "gowcaizr", "gururin", "janshin",
    "joyjoy", "kotm", "leaguebo", "legendos", "puzzledp", "puzzldpr",
    "savagere", "kizuna", "sdodgeb", "socbrawl", "ssideki", "stakes",
    "strhoop", "superspy", "3countb", "2020bb", "fbfrenzy", "ridhero",
)

MAX_SINGLE_ARCADE_CHIP_BYTES = 128 * 1024 * 1024  # 128 MiB ceiling per ROM chip


def _matches_prefix_table(stem: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if stem == prefix or stem.startswith(prefix):
            return True
    return False


def _match_shortname(archive_stem: str) -> str | None:
    """Match normalized archive stem against known CPS3 / CPS2 / CPS1 / NeoGeo shortnames."""
    stem = archive_stem.strip().lower()
    if not stem or " " in stem:
        # Full human-readable titles ("Street Fighter II (USA)") rely on chip heuristics
        return None

    # Special CPS1 exception before CPS2 `sfz*` check: `sfzch` is CPS1 Street Fighter Zero CPS Changer
    if stem.startswith("sfzch"):
        return "cps1"

    if _matches_prefix_table(stem, CPS3_PREFIXES):
        return "cps3"
    if _matches_prefix_table(stem, CPS2_EXACT_OR_PREFIXES):
        return "cps2"
    if _matches_prefix_table(stem, CPS1_EXACT_OR_PREFIXES):
        return "cps1"
    if _matches_prefix_table(stem, NEOGEO_PREFIXES):
        return "neogeo"
    return None


def _is_arcade_chip_entry(entry: ArchiveEntry) -> bool:
    """Return True if an archive entry looks like an arcade IC/PROM/EPROM dump."""
    if entry.size <= 0 or entry.size > MAX_SINGLE_ARCADE_CHIP_BYTES:
        return False

    ext = entry.suffix
    fname = entry.filename.lower()

    if ext in ARCADE_CHIP_EXPLICIT_EXTS:
        return True
    if ext and _CHIP_EXT_RE.match(ext):
        return True
    if not ext and (_CHIP_NAME_RE.match(fname) or fname in _CPS3_SIMM_NAMES):
        return True
    return False


def _detect_subplatform_by_chips(chip_entries: list[ArchiveEntry]) -> tuple[str | None, str]:
    """Inspect internal chip filenames/sizes to pinpoint `cps3`, `cps2`, or `neogeo`."""
    filenames = [e.filename.lower() for e in chip_entries]

    # 1. Neo Geo: `NNN-p1`, `NNN-c1`, `sfix.sfix`, `000-lo.lo`, etc.
    ng_hits = sum(
        1 for fn in filenames
        if _NEOGEO_CHIP_RE.match(fn) or fn in _NEOGEO_BIOS_FILES or fn.startswith("uni-bios")
    )
    if ng_hits >= 2 or any(fn in _NEOGEO_BIOS_FILES for fn in filenames):
        return "neogeo", f"Matched SNK Neo Geo MVS/AES chip layout ({ng_hits} chip hits)"

    # 2. CPS3: SIMM bank files (`10`, `20`, `30`, ...) or `cps3boot`
    simm_hits = sum(1 for fn in filenames if fn in _CPS3_SIMM_NAMES or "simm" in fn)
    if simm_hits >= 2:
        return "cps3", f"Matched Capcom CPS-3 SIMM ROM banks ({simm_hits} banks)"

    # 3. CPS2: 20-byte `.key` encryption file or QSound `.q0*`/`.m0*` + `.03*`/`.04*`
    has_cps2_key = any(e.suffix == ".key" and e.size <= 64 for e in chip_entries)
    if has_cps2_key:
        return "cps2", "Found Capcom CPS-2 encryption `.key` chip"

    has_cps2_prog = any(_CPS2_PROG_RE.search(fn) for fn in filenames)
    has_cps2_qsnd = any(_CPS2_QSOUND_RE.search(fn) for fn in filenames)
    if has_cps2_prog and has_cps2_qsnd:
        return "cps2", "Matched Capcom CPS-2 program + QSound ROM layout"

    return None, ""


def classify_arcade_archive(
    archive_stem: str,
    entries: tuple[ArchiveEntry, ...] | list[ArchiveEntry],
) -> tuple[bool, str | None, DetectionMethod | None, str]:
    """
    Determine whether an archive is an Arcade ROM set and classify its sub-platform.

    Returns `(is_arcade, system_id, detection_method, reason)`.
    If `is_arcade` is False, returns `(False, None, None, reason)`.
    """
    files = [e for e in entries if not e.is_dir]
    if not files:
        return False, None, None, "Archive contains no files"

    # Gate 1: Any unambiguous console/disc/non-ROM extension immediately disqualifies Arcade
    for entry in files:
        if entry.suffix in NON_ARCADE_EXTENSIONS:
            return (
                False,
                None,
                None,
                f"Contains console/non-arcade file '{entry.filename}' ({entry.suffix})",
            )

    # Filter out harmless sidecar text/readme files
    candidate_chips = [e for e in files if e.suffix not in IGNORABLE_SIDECAR_EXTENSIONS]
    if not candidate_chips:
        return False, None, None, "Archive contains only sidecar text/image files"

    # Gate 2: Directory depth check (MAME/FBNeo sets are flat or max 1 clone subfolder deep)
    max_depth = max(e.path.count("/") for e in candidate_chips)
    if max_depth > 1:
        return False, None, None, f"Internal directory depth ({max_depth}) exceeds arcade set layout"

    # Gate 3: Protect single-file `.bin` console ROMs (e.g. Sega Genesis or raw single-track CD)
    shortname_hit = _match_shortname(archive_stem)
    if len(candidate_chips) == 1:
        only = candidate_chips[0]
        if shortname_hit is None or only.size > 16 * 1024 * 1024:
            return (
                False,
                None,
                None,
                f"Single-file archive ('{only.filename}') treated as console/raw image, not multi-chip arcade",
            )

    # Gate 4: Chip pattern ratio check
    chip_matches = sum(1 for e in candidate_chips if _is_arcade_chip_entry(e))
    chip_ratio = chip_matches / len(candidate_chips)
    if chip_ratio < 0.6 or any(e.size > MAX_SINGLE_ARCADE_CHIP_BYTES for e in candidate_chips):
        return (
            False,
            None,
            None,
            f"Chip heuristic ratio too low ({chip_matches}/{len(candidate_chips)} matched)",
        )

    # Confirmed Arcade Set! Determine exact Cocoon FE target (`cps1`, `cps2`, `cps3`, `neogeo`, `fbneo`)
    chip_sys, chip_reason = _detect_subplatform_by_chips(candidate_chips)
    if chip_sys is not None:
        return (
            True,
            chip_sys,
            DetectionMethod.ARCADE_CHIP_HEURISTIC,
            f"{chip_reason} ({len(candidate_chips)} ROM chips)",
        )

    if shortname_hit is not None:
        return (
            True,
            shortname_hit,
            DetectionMethod.ARCADE_SHORTNAME,
            f"Matched FBNeo/MAME shortname '{archive_stem.lower()}' -> {shortname_hit} ({len(candidate_chips)} ROM chips)",
        )

    return (
        True,
        "fbneo",
        DetectionMethod.ARCADE_CHIP_HEURISTIC,
        f"Matched multi-chip Arcade ROM pattern ({chip_matches}/{len(candidate_chips)} chips) -> fbneo",
    )
