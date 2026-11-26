#!/usr/bin/env python3
"""
Völundr — Optional Guided Setup & External Tool Installer.

Uses only the Python 3.11+ standard library so beginners can run it BEFORE
creating a virtual environment or installing `requirements.txt`:

    python setup_volundr.py

What it can do (all steps are optional and non-destructive):
  1. Create `.venv` and install `requirements.txt`.
  2. Copy `config.example.toml` -> `config.toml` and set `library_root`
     (never overwrites an existing `config.toml`).
  3. Download official upstream third-party CLI tools into `./tools/`
     (skipping anything already on your `PATH` or in `./tools/`).
  4. Run `main.py doctor` to verify your finished setup.

NOTE: Console decryption keys (`keys.txt`, `aes_keys.txt`, `boot9.bin`) and
license databases (`PSV_*.tsv`) are NEVER downloaded by this script. You must
supply your own files dumped from hardware you own.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Callable
import urllib.error
import urllib.request
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_EXAMPLE = PROJECT_ROOT / "config.example.toml"
CONFIG_FILE = PROJECT_ROOT / "config.toml"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
TOOLS_DIR = PROJECT_ROOT / "tools"

USER_AGENT = "Volundr-Setup/1.0 (+https://github.com/YoungLordAce/volundr)"

TOOL_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("7z", "7-Zip (7z/7zz)", "Extracting .7z / .rar / .zip archives (recommended for all systems)"),
    ("chdman", "chdman (MAME)", "PS1 single-disc, PS2, PSP, Saturn, Dreamcast, Sega CD -> .chd"),
    ("psxpackager", "PSXPackager", "PS1 multi-disc -> single .pbp"),
    ("dolphin", "DolphinTool", "GameCube & Wii -> .rvz"),
    ("switch", "NSC_Builder / nsz", "Nintendo Switch -> merged .xci & .nsz decompression"),
    ("wiiu", "ZArchive & CDecrypt", "Nintendo Wii U -> .wua packing & NUS decryption"),
    ("psvita", "pkg2zip", "PlayStation Vita -> .pkg to Vita3K .zip"),
)

ALL_TOOL_IDS: tuple[str, ...] = tuple(item[0] for item in TOOL_GROUPS)


@dataclass(frozen=True)
class ReleaseAsset:
    """Metadata for an official upstream release download."""

    name: str
    url: str
    release_page: str


@dataclass(frozen=True)
class ToolStepResult:
    """Outcome summary for one tool installation step."""

    tool_id: str
    status: str  # "installed", "skipped", "dry-run", "manual", "failed"
    detail: str


def detect_platform_key(raw_platform: str | None = None) -> str:
    plat = (raw_platform or sys.platform).lower()
    return "windows" if plat.startswith("win") else "linux"


def detect_arch(raw_machine: str | None = None) -> str:
    mach = (raw_machine or platform.machine()).lower()
    if mach in ("amd64", "x86_64", "x64"):
        return "x64"
    if mach in ("arm64", "aarch64"):
        return "arm64"
    return mach or "unknown"


def _venv_python_path(project_root: Path = PROJECT_ROOT, platform_key: str | None = None) -> Path:
    plat = platform_key or detect_platform_key()
    if plat == "windows":
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"


# ------------------------------------------------------------------------------
# Config Generation (`config.example.toml` -> `config.toml`)
# ------------------------------------------------------------------------------
def _format_toml_path_literal(path_value: str, platform_key: str) -> str:
    """Format a path safely as a TOML string literal."""
    cleaned = path_value.strip()
    if platform_key == "windows" and "'" not in cleaned:
        return f"'{cleaned}'"
    escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_library_root_in_toml(toml_text: str, platform_key: str, new_library_root: str) -> str:
    """
    Replace `library_root = ...` inside `[paths.<platform_key>]` while preserving all comments
    and formatting, then validate the output parses as valid TOML.
    """
    target_section = f"[paths.{platform_key}]"
    lines = toml_text.splitlines(keepends=True)
    in_section = False
    replaced = False
    formatted_val = _format_toml_path_literal(new_library_root, platform_key)

    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == target_section
        elif in_section and not replaced and re.match(r"^library_root\s*=", stripped):
            eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            new_lines.append(f"library_root = {formatted_val}{eol}")
            replaced = True
            continue
        new_lines.append(line)

    if not replaced:
        raise ValueError(f"Could not find `library_root` under `{target_section}` in template.")

    updated = "".join(new_lines)
    # Validate syntax strictly before returning
    tomllib.loads(updated)
    return updated


def setup_config_file(
    *,
    project_root: Path = PROJECT_ROOT,
    platform_key: str,
    library_root: str | None = None,
    interactive: bool = True,
    dry_run: bool = False,
) -> Path:
    """Copy `config.example.toml` -> `config.toml` (if absent) and optionally set `library_root`."""
    example_path = project_root / "config.example.toml"
    config_path = project_root / "config.toml"

    if config_path.is_file():
        print(f"  [SKIP] Existing `{config_path.name}` found — leaving your personal config untouched.")
        return config_path

    if not example_path.is_file():
        raise FileNotFoundError(f"Missing template file: {example_path}")

    template_text = example_path.read_text(encoding="utf-8")
    chosen_root = (library_root or "").strip()

    if not chosen_root and interactive:
        sample = r"D:\Emulation\roms" if platform_key == "windows" else "/mnt/nas/games/library/roms"
        print("\n  Where should Völundr save your organized ROM library?")
        print(f"  (Example: {sample} — press Enter to keep the placeholder and edit later)")
        chosen_root = input("  library_root: ").strip().strip('"').strip("'")

    final_text = (
        update_library_root_in_toml(template_text, platform_key, chosen_root)
        if chosen_root
        else template_text
    )

    if dry_run:
        note = f" with library_root = {chosen_root!r}" if chosen_root else " (default template)"
        print(f"  [DRY-RUN] Would create `{config_path.name}` from `{example_path.name}`{note}.")
        return config_path

    config_path.write_text(final_text, encoding="utf-8")
    if chosen_root:
        print(f"  [OK] Created `{config_path.name}` with `{platform_key}` library_root = {chosen_root}")
    else:
        print(f"  [OK] Created `{config_path.name}` — remember to set `library_root` before running `watch`.")
    return config_path


# ------------------------------------------------------------------------------
# Virtual Environment Bootstrap
# ------------------------------------------------------------------------------
def setup_virtualenv(
    *,
    project_root: Path = PROJECT_ROOT,
    platform_key: str,
    dry_run: bool = False,
) -> Path:
    """Create `.venv` (if absent) and install `requirements.txt`."""
    venv_dir = project_root / ".venv"
    venv_py = _venv_python_path(project_root, platform_key)
    req_file = project_root / "requirements.txt"

    if dry_run:
        if not venv_py.is_file():
            print(f"  [DRY-RUN] Would create virtual environment at `{venv_dir}`")
        print(f"  [DRY-RUN] Would install Python dependencies from `{req_file.name}` into `.venv`")
        return venv_py

    if not venv_py.is_file():
        print(f"  Creating virtual environment at `{venv_dir}`...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    else:
        print(f"  [OK] Virtual environment already exists at `{venv_dir}`.")

    if req_file.is_file():
        print("  Installing Python dependencies (`requirements.txt`)...")
        subprocess.run(
            [str(venv_py), "-m", "pip", "install", "-r", str(req_file)],
            check=True,
        )
        print("  [OK] Python dependencies installed.")
    return venv_py


# ------------------------------------------------------------------------------
# GitHub Releases API & Safe Archive Extraction Helpers
# ------------------------------------------------------------------------------
def _fetch_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_text(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def pick_github_release_asset(
    release_data: dict,
    patterns: tuple[str, ...],
    *,
    exclude_patterns: tuple[str, ...] = (),
    fallback_page: str = "",
) -> ReleaseAsset | None:
    """
    Match the first release asset whose filename matches any regex in `patterns` (ordered by
    preference) while not matching any regex in `exclude_patterns`.
    """
    assets = release_data.get("assets") or []
    release_page = str(release_data.get("html_url") or fallback_page)
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for asset in assets:
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if not name or not url:
                continue
            if any(re.search(ex, name, re.IGNORECASE) for ex in exclude_patterns):
                continue
            if rx.search(name):
                return ReleaseAsset(name=name, url=url, release_page=release_page)
    return None


def fetch_latest_github_asset(
    repo: str,
    patterns: tuple[str, ...],
    *,
    exclude_patterns: tuple[str, ...] = (),
) -> ReleaseAsset | None:
    """Query GitHub's `/releases/latest` endpoint for `owner/repo` and return a matching asset."""
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    fallback_page = f"https://github.com/{repo}/releases"
    data = _fetch_json(api_url)
    return pick_github_release_asset(
        data,
        patterns,
        exclude_patterns=exclude_patterns,
        fallback_page=fallback_page,
    )


def _download_file(url: str, dest: Path, label: str) -> Path:
    """Stream `url` to `dest` with simple progress output."""
    print(f"    Downloading {label} from:\n      {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120.0) as resp, dest.open("wb") as out:
        total_hdr = resp.headers.get("Content-Length")
        total = int(total_hdr) if total_hdr and total_hdr.isdigit() else 0
        downloaded = 0
        last_pct = -1
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int((downloaded * 100) / total)
                if pct >= last_pct + 10 or pct == 100:
                    last_pct = pct
                    mb_done = downloaded / (1024 * 1024)
                    mb_tot = total / (1024 * 1024)
                    print(f"\r    Progress: {pct:3d}% ({mb_done:.1f} / {mb_tot:.1f} MiB)", end="", flush=True)
        if total > 0:
            print()
    return dest


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract a `.zip` archive while blocking Zip-Slip path traversal."""
    resolved_dest = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            norm = info.filename.replace("\\", "/").lstrip("/")
            if not norm:
                continue
            parts = PurePosixPath(norm).parts
            if ".." in parts or (parts and re.match(r"^[A-Za-z]:", parts[0])):
                raise ValueError(f"Unsafe path in zip archive: {info.filename!r}")
            target = (resolved_dest / Path(*parts)).resolve()
            if not target.is_relative_to(resolved_dest):
                raise ValueError(f"Zip entry escapes target directory: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=256 * 1024)


def _safe_extract_tar(tar_path: Path, dest_dir: Path) -> None:
    """Extract a `.tar.gz`/`.tar.xz` archive while blocking path traversal and symlinks."""
    resolved_dest = dest_dir.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            norm = member.name.replace("\\", "/").lstrip("/")
            if not norm:
                continue
            parts = PurePosixPath(norm).parts
            if ".." in parts or (parts and re.match(r"^[A-Za-z]:", parts[0])):
                raise ValueError(f"Unsafe path in tar archive: {member.name!r}")
            if member.issym() or member.islnk():
                continue
            target = (resolved_dest / Path(*parts)).resolve()
            if not target.is_relative_to(resolved_dest):
                raise ValueError(f"Tar entry escapes target directory: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is not None:
                    with src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=256 * 1024)


def _make_executable(path: Path) -> None:
    if os.name != "nt" and path.is_file():
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _find_installed_7z(project_root: Path, platform_key: str) -> Path | None:
    """Locate an already-installed 7z/7zz binary on PATH or standard folders."""
    if platform_key == "windows":
        candidates = [
            project_root / "tools" / "7z" / "7z.exe",
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ]
        for cand in candidates:
            if cand.is_file():
                return cand
        hit = shutil.which("7z") or shutil.which("7zz")
        return Path(hit) if hit else None

    for cand in (
        project_root / "tools" / "7z" / "7zz",
        Path("/usr/local/bin/7zz"),
        Path("/usr/bin/7zz"),
    ):
        if cand.is_file():
            return cand
    hit = shutil.which("7zz") or shutil.which("7z")
    return Path(hit) if hit else None


def _copy_single_binary_from_tree(root: Path, target_names: tuple[str, ...], dest_file: Path) -> bool:
    """Search `root` recursively for any filename in `target_names` (case-insensitive) and copy to `dest_file`."""
    wanted = {n.lower() for n in target_names}
    for cand in sorted(root.rglob("*")):
        if cand.is_file() and cand.name.lower() in wanted:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cand, dest_file)
            _make_executable(dest_file)
            return True
    return False


def _copy_tree_contents(src_dir: Path, dst_dir: Path) -> None:
    """Copy contents of `src_dir` into `dst_dir`, unwrapping a single wrapper directory if present."""
    children = [p for p in src_dir.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        src_dir = children[0]
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        target = dst_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _detect_linux_pkg_manager() -> tuple[str, list[str]] | None:
    if shutil.which("dnf"):
        return "dnf", ["sudo", "dnf", "install", "-y", "7zip", "mame-tools", "dolphin-emu-tool", "dotnet-runtime-10.0"]
    if shutil.which("apt-get"):
        return "apt", ["sudo", "apt-get", "install", "-y", "7zip", "mame-tools", "dolphin-emu"]
    if shutil.which("pacman"):
        return "pacman", ["sudo", "pacman", "-S", "--needed", "7zip", "mame-tools", "dolphin-emu"]
    return None


# ------------------------------------------------------------------------------
# Per-Tool Installers
# ------------------------------------------------------------------------------
def _install_7z(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> ToolStepResult:
    existing = _find_installed_7z(project_root, platform_key)
    if existing is not None and not force:
        if platform_key == "windows" or existing.name.lower() == "7zz":
            return ToolStepResult("7z", "skipped", f"Already installed: {existing}")

    if platform_key == "windows":
        if dry_run:
            return ToolStepResult(
                "7z",
                "dry-run",
                "Would install 7-Zip via `winget install -e --id 7zip.7zip` (or guide manual install)",
            )
        winget = shutil.which("winget")
        if winget:
            print("    Installing 7-Zip via winget (`7zip.7zip`)...")
            proc = subprocess.run(
                [winget, "install", "-e", "--id", "7zip.7zip", "--accept-package-agreements", "--accept-source-agreements"],
                check=False,
            )
            if proc.returncode == 0:
                found = _find_installed_7z(project_root, platform_key)
                return ToolStepResult("7z", "installed", f"Installed via winget ({found or '7z.exe'})")
        return ToolStepResult(
            "7z",
            "manual",
            "Install 7-Zip from https://www.7-zip.org/download.html (default install path is auto-detected)",
        )

    # Linux: download official standalone `7zz` (includes full RAR/RAR5 codecs)
    if arch != "x64":
        return ToolStepResult(
            "7z",
            "manual",
            f"Unsupported auto-fetch arch `{arch}` — download `7zz` from https://github.com/ip7z/7zip/releases into tools/7z/7zz",
        )
    dest = project_root / "tools" / "7z" / "7zz"
    if dry_run:
        return ToolStepResult("7z", "dry-run", f"Would fetch latest `7z*-linux-x64.tar.xz` from ip7z/7zip -> {dest}")

    asset = fetch_latest_github_asset("ip7z/7zip", (r"7z.*linux-x64\.tar\.xz$",))
    if asset is None:
        return ToolStepResult("7z", "manual", "Could not find linux-x64 release at https://github.com/ip7z/7zip/releases")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        arc = _download_file(asset.url, tmp_path / asset.name, asset.name)
        ext_dir = tmp_path / "ext"
        _safe_extract_tar(arc, ext_dir)
        if not _copy_single_binary_from_tree(ext_dir, ("7zz",), dest):
            return ToolStepResult("7z", "failed", f"`7zz` binary not found inside `{asset.name}`")
    return ToolStepResult("7z", "installed", f"Installed `{dest}`")


def _install_chdman(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> ToolStepResult:
    dest = project_root / "tools" / "chdman" / ("chdman.exe" if platform_key == "windows" else "chdman")
    existing = dest if dest.is_file() else (Path(w) if (w := shutil.which("chdman")) else None)
    if existing is not None and not force:
        return ToolStepResult("chdman", "skipped", f"Already installed: {existing}")

    if platform_key != "windows":
        pkg_info = _detect_linux_pkg_manager()
        hint = " ".join(pkg_info[1]) if pkg_info else "install `mame-tools` via your package manager"
        return ToolStepResult("chdman", "manual", f"On Linux, install via package manager: `{hint}`")

    if dry_run:
        return ToolStepResult(
            "chdman",
            "dry-run",
            f"Would fetch latest MAME Windows x64 release from mamedev/mame and extract `chdman.exe` -> {dest}",
        )

    seven_zip = _find_installed_7z(project_root, platform_key)
    if seven_zip is None:
        return ToolStepResult(
            "chdman",
            "manual",
            "Requires 7-Zip first to extract `chdman.exe` from MAME's release archive (https://github.com/mamedev/mame/releases)",
        )

    asset = fetch_latest_github_asset("mamedev/mame", (r"mame\d+b_x64\.exe$", r"mame\d+b_64bit\.exe$"))
    if asset is None:
        return ToolStepResult("chdman", "manual", "Download MAME x64 from https://github.com/mamedev/mame/releases and copy `chdman.exe` to tools/chdman/")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sfx = _download_file(asset.url, tmp_path / asset.name, asset.name)
        ext_dir = tmp_path / "ext"
        ext_dir.mkdir()
        proc = subprocess.run(
            [str(seven_zip), "e", "-y", f"-o{ext_dir}", str(sfx), "chdman.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not _copy_single_binary_from_tree(ext_dir, ("chdman.exe",), dest):
            return ToolStepResult(
                "chdman",
                "failed",
                f"Failed extracting `chdman.exe` from `{asset.name}` — download manually from {asset.release_page}",
            )
    return ToolStepResult("chdman", "installed", f"Extracted `{dest}` from {asset.name}")


def _install_psxpackager(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> ToolStepResult:
    exe_name = "psxpackager.exe" if platform_key == "windows" else "psxpackager"
    dest_dir = project_root / "tools" / "psxpackager"
    dest_exe = dest_dir / exe_name
    existing = dest_exe if dest_exe.is_file() else (Path(w) if (w := shutil.which("psxpackager")) else None)
    if existing is not None and not force:
        return ToolStepResult("psxpackager", "skipped", f"Already installed: {existing}")

    if arch != "x64":
        return ToolStepResult(
            "psxpackager",
            "manual",
            f"Download the `{arch}` build manually from https://github.com/RupertAvery/PSXPackager/releases -> tools/psxpackager/",
        )

    patterns = (
        (r"psxpackager.*win.*x64.*\.zip$", r"win-x64.*\.zip$")
        if platform_key == "windows"
        else (r"psxpackager.*linux.*x64.*\.(?:tar\.gz|zip)$", r"linux-x64.*\.(?:tar\.gz|zip)$")
    )
    if dry_run:
        return ToolStepResult(
            "psxpackager",
            "dry-run",
            f"Would fetch latest {platform_key}-x64 CLI from RupertAvery/PSXPackager -> {dest_dir}",
        )

    asset = fetch_latest_github_asset(
        "RupertAvery/PSXPackager",
        patterns,
        exclude_patterns=(r"gui",),
    )
    if asset is None:
        return ToolStepResult(
            "psxpackager",
            "manual",
            "Download CLI release manually from https://github.com/RupertAvery/PSXPackager/releases -> tools/psxpackager/",
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        arc = _download_file(asset.url, tmp_path / asset.name, asset.name)
        ext_dir = tmp_path / "ext"
        ext_dir.mkdir()
        if arc.name.lower().endswith(".zip"):
            _safe_extract_zip(arc, ext_dir)
        else:
            _safe_extract_tar(arc, ext_dir)
        _copy_tree_contents(ext_dir, dest_dir)
        _make_executable(dest_exe)

    if not dest_exe.is_file():
        return ToolStepResult("psxpackager", "failed", f"Extracted `{asset.name}`, but `{dest_exe}` was not found")
    return ToolStepResult("psxpackager", "installed", f"Installed `{dest_exe}`")


def _find_latest_dolphin_win_url() -> str | None:
    """
    Discover the latest Dolphin Windows x64 `.7z` download URL using Dolphin's official
    JSON update API (`https://dolphin-emu.org/update/latest/beta`), falling back to the
    download page only if needed.
    """
    try:
        meta = _fetch_json("https://dolphin-emu.org/update/latest/beta")
        shortrev = str(meta.get("shortrev") or meta.get("version") or "").strip()
        if shortrev and re.fullmatch(r"[A-Za-z0-9._-]+", shortrev):
            return f"https://dl.dolphin-emu.org/releases/{shortrev}/dolphin-{shortrev}-x64.7z"
    except Exception:
        pass

    try:
        html = _fetch_text("https://dolphin-emu.org/download/")
        matches = re.findall(r"https://dl\.dolphin-emu\.org/releases/[^\s\"'<>]+-x64\.7z", html)
        return matches[0] if matches else None
    except Exception:
        return None


def _install_dolphin(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> ToolStepResult:
    if platform_key == "windows":
        candidates = [
            project_root / "tools" / "dolphin" / "DolphinTool.exe",
            Path(r"C:\Program Files\Dolphin-x64\DolphinTool.exe"),
        ]
        existing = next((c for c in candidates if c.is_file()), None) or (
            Path(w) if (w := shutil.which("DolphinTool")) else None
        )
    else:
        existing = Path(w) if (w := shutil.which("dolphin-tool") or shutil.which("DolphinTool")) else None

    if existing is not None and not force:
        return ToolStepResult("dolphin", "skipped", f"Already installed: {existing}")

    if platform_key != "windows":
        pkg_info = _detect_linux_pkg_manager()
        hint = " ".join(pkg_info[1]) if pkg_info else "install `dolphin-emu-tool` via your package manager"
        return ToolStepResult("dolphin", "manual", f"On Linux, install via package manager: `{hint}`")

    dest_dir = project_root / "tools" / "dolphin"
    dest_exe = dest_dir / "DolphinTool.exe"
    if dry_run:
        return ToolStepResult(
            "dolphin",
            "dry-run",
            f"Would fetch latest Dolphin Windows x64 `.7z` from dolphin-emu.org -> {dest_exe}",
        )

    seven_zip = _find_installed_7z(project_root, platform_key)
    if seven_zip is None:
        return ToolStepResult(
            "dolphin",
            "manual",
            "Requires 7-Zip first to extract Dolphin `.7z` — or download manually from https://dolphin-emu.org/download/ -> tools/dolphin/",
        )

    url = _find_latest_dolphin_win_url()
    if not url:
        return ToolStepResult(
            "dolphin",
            "manual",
            "Download Dolphin Windows x64 from https://dolphin-emu.org/download/ and extract into tools/dolphin/",
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        arc = _download_file(url, tmp_path / Path(url).name, Path(url).name)
        ext_dir = tmp_path / "ext"
        ext_dir.mkdir()
        proc = subprocess.run(
            [str(seven_zip), "x", "-y", f"-o{ext_dir}", str(arc)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return ToolStepResult("dolphin", "failed", f"7z failed extracting `{arc.name}`")
        _copy_tree_contents(ext_dir, dest_dir)

    if not dest_exe.is_file():
        return ToolStepResult("dolphin", "failed", f"Extracted Dolphin, but `{dest_exe}` was not found")
    return ToolStepResult("dolphin", "installed", f"Installed `{dest_exe}`")


def _install_simple_github_win_tool(
    *,
    tool_id: str,
    repo: str,
    exe_name: str,
    dest_file: Path,
    asset_patterns: tuple[str, ...],
    platform_key: str,
    force: bool,
    dry_run: bool,
    exclude_patterns: tuple[str, ...] = (),
) -> ToolStepResult:
    """Install a single-binary Windows CLI tool from GitHub Releases (skipping if present)."""
    existing = dest_file if dest_file.is_file() else (
        Path(w) if (w := shutil.which(dest_file.stem)) else None
    )
    if existing is not None and not force:
        return ToolStepResult(tool_id, "skipped", f"Already installed: {existing}")

    if platform_key != "windows":
        return ToolStepResult(
            tool_id,
            "manual",
            f"On Linux, build from https://github.com/{repo} (or use the included Dockerfile, which compiles it automatically)",
        )

    if dry_run:
        return ToolStepResult(
            tool_id,
            "dry-run",
            f"Would fetch latest `{exe_name}` from {repo} -> {dest_file}",
        )

    asset = fetch_latest_github_asset(repo, asset_patterns, exclude_patterns=exclude_patterns)
    if asset is None:
        return ToolStepResult(
            tool_id,
            "manual",
            f"Download `{exe_name}` manually from https://github.com/{repo}/releases -> {dest_file.parent}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dl = _download_file(asset.url, tmp_path / asset.name, asset.name)
        if dl.suffix.lower() == ".exe":
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dl, dest_file)
        elif dl.suffix.lower() == ".zip":
            ext_dir = tmp_path / "ext"
            ext_dir.mkdir()
            _safe_extract_zip(dl, ext_dir)
            if not _copy_single_binary_from_tree(ext_dir, (exe_name,), dest_file):
                return ToolStepResult(
                    tool_id,
                    "failed",
                    f"`{exe_name}` not found inside `{asset.name}` — check {asset.release_page}",
                )
        else:
            return ToolStepResult(
                tool_id,
                "manual",
                f"Unsupported archive format `{asset.name}` — download manually from {asset.release_page}",
            )
    return ToolStepResult(tool_id, "installed", f"Installed `{dest_file}`")


def _install_wiiu(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> list[ToolStepResult]:
    _ = arch
    z_res = _install_simple_github_win_tool(
        tool_id="wiiu/zarchive",
        repo="Exzap/ZArchive",
        exe_name="zarchive.exe",
        dest_file=project_root / "tools" / "wiiu" / "zarchive.exe",
        asset_patterns=(r"zarchive.*win.*\.zip$", r"zarchive.*\.exe$", r"\.zip$"),
        platform_key=platform_key,
        force=force,
        dry_run=dry_run,
    )
    c_res = _install_simple_github_win_tool(
        tool_id="wiiu/cdecrypt",
        repo="VitaSmith/cdecrypt",
        exe_name="cdecrypt.exe",
        dest_file=project_root / "tools" / "wiiu" / "cdecrypt.exe",
        asset_patterns=(r"cdecrypt.*win.*\.zip$", r"cdecrypt.*\.exe$", r"\.zip$"),
        platform_key=platform_key,
        force=force,
        dry_run=dry_run,
    )
    return [z_res, c_res]


def _install_psvita(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> ToolStepResult:
    _ = arch
    return _install_simple_github_win_tool(
        tool_id="psvita/pkg2zip",
        repo="lusid1/pkg2zip",
        exe_name="pkg2zip.exe",
        dest_file=project_root / "tools" / "psvita" / "pkg2zip.exe",
        asset_patterns=(
            r"pkg2zip.*win64.*\.zip$",
            r"pkg2zip.*x64.*\.zip$",
            r"pkg2zip.*win.*\.zip$",
            r"^pkg2zip\.zip$",
            r"pkg2zip.*\.zip$",
        ),
        exclude_patterns=(r"linux", r"mac", r"darwin", r"arm", r"source"),
        platform_key=platform_key,
        force=force,
        dry_run=dry_run,
    )


def _install_switch(project_root: Path, platform_key: str, arch: str, force: bool, dry_run: bool) -> ToolStepResult:
    _ = arch
    if platform_key == "windows":
        dest_dir = project_root / "tools" / "nscb"
        dest_exe = dest_dir / "ztools" / "squirrel.exe"
        if dest_exe.is_file() and not force:
            return ToolStepResult("switch", "skipped", f"Already installed: {dest_exe}")
        if dry_run:
            return ToolStepResult(
                "switch",
                "dry-run",
                f"Would fetch latest NSC_Builder Windows archive from julesontheroad/NSC_BUILDER -> {dest_dir}",
            )
        asset = fetch_latest_github_asset(
            "julesontheroad/NSC_BUILDER",
            (r"NSCB.*x64.*\.zip$", r"NSCB.*\.zip$", r"NSCB.*x64.*\.7z$", r"NSCB.*\.7z$"),
        )
        if asset is None:
            return ToolStepResult(
                "switch",
                "manual",
                "Download NSC_Builder manually from https://github.com/julesontheroad/NSC_BUILDER/releases -> tools/nscb/",
            )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            arc = _download_file(asset.url, tmp_path / asset.name, asset.name)
            ext_dir = tmp_path / "ext"
            ext_dir.mkdir()
            if arc.suffix.lower() == ".zip":
                _safe_extract_zip(arc, ext_dir)
            else:
                seven_zip = _find_installed_7z(project_root, platform_key)
                if seven_zip is None:
                    return ToolStepResult(
                        "switch",
                        "manual",
                        f"Requires 7-Zip to extract `{asset.name}` — download from {asset.release_page}",
                    )
                proc = subprocess.run(
                    [str(seven_zip), "x", "-y", f"-o{ext_dir}", str(arc)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode != 0:
                    return ToolStepResult("switch", "failed", f"7z failed extracting `{asset.name}`")
            _copy_tree_contents(ext_dir, dest_dir)
        if not dest_exe.is_file():
            return ToolStepResult(
                "switch",
                "manual",
                f"Extracted `{asset.name}` into `{dest_dir}`, but `ztools/squirrel.exe` was not found at expected path",
            )
        return ToolStepResult(
            "switch",
            "installed",
            f"Installed `{dest_exe}` (drop your `prod.keys` or `keys.txt` in `tools/nscb/ztools/` or `~/.switch/prod.keys`)",
        )

    # Linux: install `nsz` inside `.venv` for `.nsz`/`.xcz` decompression
    venv_py = _venv_python_path(project_root, platform_key)
    nsz_bin = venv_py.parent / "nsz"
    if (nsz_bin.is_file() or shutil.which("nsz")) and not force:
        return ToolStepResult("switch", "skipped", f"Already installed: {nsz_bin if nsz_bin.is_file() else shutil.which('nsz')}")
    if dry_run:
        return ToolStepResult("switch", "dry-run", "Would install `nsz` via pip inside `.venv`")
    if not venv_py.is_file():
        return ToolStepResult("switch", "manual", "Create `.venv` first, then run `./.venv/bin/pip install nsz`")
    proc = subprocess.run([str(venv_py), "-m", "pip", "install", "nsz"], check=False)
    if proc.returncode != 0:
        return ToolStepResult("switch", "failed", "Failed installing `nsz` via pip")
    return ToolStepResult(
        "switch",
        "installed",
        "Installed `nsz` in `.venv` (for full multi-file `.xci` merging on Linux, see DOCUMENTATION.md / Dockerfile)",
    )


def install_selected_tools(
    selected_tools: list[str],
    *,
    project_root: Path = PROJECT_ROOT,
    platform_key: str,
    arch: str,
    force: bool = False,
    dry_run: bool = False,
) -> list[ToolStepResult]:
    """Install the requested tool IDs in dependency order (7z always first)."""
    ordered = [tid for tid in ALL_TOOL_IDS if tid in selected_tools]
    results: list[ToolStepResult] = []

    handlers: dict[str, Callable[[Path, str, str, bool, bool], ToolStepResult | list[ToolStepResult]]] = {
        "7z": _install_7z,
        "chdman": _install_chdman,
        "psxpackager": _install_psxpackager,
        "dolphin": _install_dolphin,
        "switch": _install_switch,
        "wiiu": _install_wiiu,
        "psvita": _install_psvita,
    }

    for tid in ordered:
        print(f"\n  [{tid}] Checking / installing...")
        try:
            outcome = handlers[tid](project_root, platform_key, arch, force, dry_run)
        except urllib.error.HTTPError as exc:
            err_url = str(getattr(exc, "url", "") or getattr(exc, "filename", "") or "").lower()
            if exc.code in (403, 429) and "github.com" in err_url:
                note = "GitHub API rate limit hit (HTTP 403) — try again later or download from README.md"
            else:
                note = f"HTTP error {exc.code}: {exc.reason}"
            outcome = ToolStepResult(tid, "failed", note)
        except Exception as exc:
            outcome = ToolStepResult(tid, "failed", str(exc))

        items = outcome if isinstance(outcome, list) else [outcome]
        for res in items:
            tag = res.status.upper()
            print(f"    [{tag}] {res.tool_id}: {res.detail}")
            results.append(res)

    return results


# ------------------------------------------------------------------------------
# Interactive Prompts & CLI
# ------------------------------------------------------------------------------
def _prompt_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    ans = input(f"{prompt} {suffix}: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _prompt_tool_selection() -> list[str]:
    print("\nWhich external tools would you like to install into `./tools/`?")
    print("  [A] All tools (recommended — skips anything you already have)")
    print("  [C] Choose one by one")
    print("  [N] None (skip external tools for now)")
    choice = input("Selection [A/c/n]: ").strip().lower() or "a"
    if choice.startswith("n"):
        return []
    if choice.startswith("c"):
        picked: list[str] = []
        for tid, label, desc in TOOL_GROUPS:
            if _prompt_yes_no(f"  Install {label} ({desc})?", default=True):
                picked.append(tid)
        return picked
    return list(ALL_TOOL_IDS)


def _ensure_key_placeholder_dirs(project_root: Path, dry_run: bool) -> None:
    for rel in ("tools/3ds", "tools/nscb/ztools", "tools/psvita"):
        folder = project_root / rel
        if not dry_run:
            folder.mkdir(parents=True, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_volundr.py",
        description="Guided first-time setup & official external tool installer for Völundr.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Non-interactive mode (accept defaults without prompting)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview every step without creating files or downloading tools",
    )
    parser.add_argument(
        "--library-root",
        default=None,
        help="Destination ROM library path to write into a newly created config.toml",
    )
    parser.add_argument(
        "--tools",
        default=None,
        help=f"Comma-separated tool IDs to install ({','.join(ALL_TOOL_IDS)}), or 'all' / 'none'",
    )
    parser.add_argument("--skip-venv", action="store_true", help="Skip `.venv` creation & pip install")
    parser.add_argument("--skip-config", action="store_true", help="Skip `config.toml` creation")
    parser.add_argument("--skip-tools", action="store_true", help="Skip downloading external tools")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download selected tools even if already present (never overwrites config.toml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if sys.version_info < (3, 11):
        print(
            f"[ERROR] Völundr requires Python 3.11+ (detected {sys.version.split()[0]}).",
            file=sys.stderr,
        )
        return 1

    args = build_parser().parse_args(argv)
    plat = detect_platform_key()
    arch = detect_arch()
    interactive = not args.yes and sys.stdin.isatty()

    print("=" * 72)
    print(f"Völundr — Guided Setup ({plat}/{arch})")
    if args.dry_run:
        print("MODE: DRY-RUN (no files will be created or downloaded)")
    print("=" * 72)

    # 1. Virtual environment & Python dependencies
    venv_py = _venv_python_path(PROJECT_ROOT, plat)
    if not args.skip_venv:
        print("\n[Step 1/4] Python Virtual Environment & Dependencies")
        if not interactive or _prompt_yes_no("  Set up `.venv` and install `requirements.txt`?", default=True):
            try:
                venv_py = setup_virtualenv(project_root=PROJECT_ROOT, platform_key=plat, dry_run=args.dry_run)
            except subprocess.CalledProcessError as exc:
                print(f"  [ERROR] Virtualenv/pip step failed (exit {exc.returncode}).", file=sys.stderr)
                return 1

    # 2. Configuration (`config.toml`)
    if not args.skip_config:
        print("\n[Step 2/4] Local Configuration (`config.toml`)")
        setup_config_file(
            project_root=PROJECT_ROOT,
            platform_key=plat,
            library_root=args.library_root,
            interactive=interactive,
            dry_run=args.dry_run,
        )

    # 3. External tools
    if not args.skip_tools:
        print("\n[Step 3/4] External CLI Tools (`./tools/`)")
        print("  Tools are fetched directly from official upstream GitHub releases.")
        if args.tools is not None:
            raw = args.tools.strip().lower()
            if raw == "all":
                selected = list(ALL_TOOL_IDS)
            elif raw in ("none", ""):
                selected = []
            else:
                selected = [t.strip().lower() for t in raw.split(",") if t.strip()]
                unknown = [t for t in selected if t not in ALL_TOOL_IDS]
                if unknown:
                    print(f"  [ERROR] Unknown tool ID(s): {', '.join(unknown)}. Valid: {', '.join(ALL_TOOL_IDS)}", file=sys.stderr)
                    return 2
        elif interactive:
            selected = _prompt_tool_selection()
        else:
            selected = list(ALL_TOOL_IDS)

        if selected:
            install_selected_tools(
                selected,
                project_root=PROJECT_ROOT,
                platform_key=plat,
                arch=arch,
                force=args.force,
                dry_run=args.dry_run,
            )
        else:
            print("  [SKIP] External tool downloads skipped.")

        if plat == "linux":
            pkg_info = _detect_linux_pkg_manager()
            if pkg_info:
                print(f"\n  Tip (Linux {pkg_info[0]}): Install native packages with:")
                print(f"    {' '.join(pkg_info[1])}")

    _ensure_key_placeholder_dirs(PROJECT_ROOT, dry_run=args.dry_run)

    # 4. Reminder for user-supplied keys + `main.py doctor`
    print("\n[Step 4/4] Verification & User-Supplied Keys Reminder")
    print("  Völundr NEVER downloads copyrighted console keys or license DBs.")
    print("  If you ingest these consoles, drop your own legally dumped files into:")
    print("    - Switch:  tools/nscb/ztools/prod.keys  (or keys.txt / ~/.switch/prod.keys — auto-synced)")
    print("    - 3DS:     tools/3ds/aes_keys.txt  (or tools/3ds/boot9.bin)")
    print("    - PS Vita: tools/psvita/PSV_GAMES.tsv  (optional)")

    if args.dry_run:
        print("\n  [DRY-RUN] Would run `main.py doctor` to verify your setup.")
        return 0

    run_py = venv_py if venv_py.is_file() else Path(sys.executable)
    if CONFIG_FILE.is_file():
        print("\nRunning `main.py doctor`...\n")
        proc = subprocess.run([str(run_py), str(PROJECT_ROOT / "main.py"), "doctor"], check=False)
        return proc.returncode

    print("\nSetup complete! Copy `config.example.toml` -> `config.toml`, then run `main.py doctor`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
