"""GitHub release update checker and safe one-click `git pull` updater for Völundr."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from src import __version__

GITHUB_REPO = "YoungLordAce/volundr"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_ATOM_URL = f"https://github.com/{GITHUB_REPO}/releases.atom"
RELEASES_WEB_URL = f"https://github.com/{GITHUB_REPO}/releases"
USER_AGENT = f"Volundr/{__version__} (+https://github.com/{GITHUB_REPO})"

_CACHE_TTL_SECONDS = 300.0
_cached_etag: str | None = None
_cached_result: UpdateCheckResult | None = None
_cached_at: float = 0.0


@dataclass(frozen=True)
class UpdateCheckResult:
    """Result of comparing the local Völundr version against the latest GitHub release."""

    current_version: str
    latest_version: str
    update_available: bool
    release_title: str
    release_notes: str
    release_url: str
    error: str | None = None
    release_notes_is_html: bool = False


@dataclass(frozen=True)
class UpdateApplyResult:
    """Result of applying a one-click `git pull --ff-only` + pip refresh."""

    success: bool
    message: str
    needs_restart: bool = False


def parse_semver(raw: str) -> tuple[int, ...]:
    """Parse `'v0.3.1'` or `'0.3.1-beta'` into `(0, 3, 1)` for reliable version comparison."""
    cleaned = raw.strip().lstrip("vV")
    nums = re.findall(r"\d+", cleaned.split("-", 1)[0])
    if not nums:
        return (0,)
    return tuple(int(n) for n in nums[:3])


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if `latest` is strictly greater than `current`."""
    l_tup = parse_semver(latest)
    c_tup = parse_semver(current)
    max_len = max(len(l_tup), len(c_tup))
    l_pad = l_tup + (0,) * (max_len - len(l_tup))
    c_pad = c_tup + (0,) * (max_len - len(c_tup))
    return l_pad > c_pad


def parse_releases_atom_xml(
    xml_text: str,
    current_version: str = __version__,
) -> UpdateCheckResult | None:
    """Parse GitHub's public `releases.atom` feed (immune to `api.github.com` 60 req/hr rate limits)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        return None

    link_el = entry.find("atom:link", ns)
    url = (link_el.attrib.get("href") if link_el is not None else None) or RELEASES_WEB_URL
    url = url.strip()

    title_el = entry.find("atom:title", ns)
    title = (title_el.text or "").strip() if title_el is not None else ""

    # Extract version tag from `/releases/tag/<tag>` URL first, falling back to entry title
    tag_match = re.search(r"/releases/tag/([^/?#]+)", url)
    raw_tag = tag_match.group(1) if tag_match else title
    latest = raw_tag.strip().lstrip("vV") or current_version
    if not title:
        title = raw_tag or f"v{latest}"

    content_el = entry.find("atom:content", ns)
    content = (content_el.text or "").strip() if content_el is not None else ""
    is_html = (content_el.attrib.get("type", "").lower() == "html") if content_el is not None else False

    return UpdateCheckResult(
        current_version=current_version,
        latest_version=latest,
        update_available=is_newer_version(latest, current_version),
        release_title=title,
        release_notes=content,
        release_url=url,
        release_notes_is_html=is_html,
    )


def _check_via_atom_feed(current_version: str, timeout: float) -> UpdateCheckResult | None:
    req = urllib.request.Request(
        RELEASES_ATOM_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    return parse_releases_atom_xml(xml_text, current_version=current_version)


def clear_update_cache() -> None:
    """Reset the in-memory update check cache and ETag (primarily for tests)."""
    global _cached_etag, _cached_result, _cached_at
    _cached_etag = None
    _cached_result = None
    _cached_at = 0.0


def check_for_updates(
    current_version: str = __version__,
    *,
    timeout: float = 12.0,
    bypass_cache: bool = False,
) -> UpdateCheckResult:
    """
    Query GitHub for the latest published Völundr release.

    Uses `ETag` conditional requests (`304 Not Modified` costs 0 API quota), caches fresh
    results briefly in memory, and automatically falls back to GitHub's public `releases.atom`
    feed if `api.github.com` returns an HTTP 403/429 rate-limit error.
    """
    global _cached_etag, _cached_result, _cached_at

    now = time.monotonic()
    if (
        not bypass_cache
        and _cached_result is not None
        and _cached_result.error is None
        and _cached_result.current_version == current_version
        and (now - _cached_at) < _CACHE_TTL_SECONDS
    ):
        return _cached_result

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    if _cached_etag and _cached_result is not None and _cached_result.current_version == current_version:
        headers["If-None-Match"] = _cached_etag

    req = urllib.request.Request(RELEASES_API_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            new_etag = resp.headers.get("ETag")
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and _cached_result is not None:
            _cached_at = now
            return _cached_result
        if exc.code in (403, 429):
            # Fallback to public `releases.atom` feed (does not share `api.github.com` 60/hr limit)
            atom_res = _check_via_atom_feed(current_version, timeout)
            if atom_res is not None:
                _cached_result = atom_res
                _cached_at = now
                return atom_res
            return UpdateCheckResult(
                current_version=current_version,
                latest_version=current_version,
                update_available=False,
                release_title="",
                release_notes="",
                release_url=RELEASES_WEB_URL,
                error="GitHub API rate limit reached (HTTP 403) and Atom feed fallback failed.",
            )
        if exc.code == 404:
            return UpdateCheckResult(
                current_version=current_version,
                latest_version=current_version,
                update_available=False,
                release_title="",
                release_notes="",
                release_url=RELEASES_WEB_URL,
                error="No published GitHub Releases found yet.",
            )
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=current_version,
            update_available=False,
            release_title="",
            release_notes="",
            release_url=RELEASES_WEB_URL,
            error=f"HTTP {exc.code}: {exc.reason}",
        )
    except Exception as exc:
        atom_res = _check_via_atom_feed(current_version, timeout)
        if atom_res is not None:
            _cached_result = atom_res
            _cached_at = now
            return atom_res
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=current_version,
            update_available=False,
            release_title="",
            release_notes="",
            release_url=RELEASES_WEB_URL,
            error=f"Could not reach GitHub: {exc}",
        )

    tag = str(data.get("tag_name") or "").strip()
    latest = tag.lstrip("vV") or current_version
    title = str(data.get("name") or tag or f"v{latest}").strip()
    body = str(data.get("body") or "").strip()
    url = str(data.get("html_url") or RELEASES_WEB_URL).strip()

    result = UpdateCheckResult(
        current_version=current_version,
        latest_version=latest,
        update_available=is_newer_version(latest, current_version),
        release_title=title,
        release_notes=body,
        release_url=url,
        release_notes_is_html=False,
    )
    if new_etag:
        _cached_etag = new_etag
    _cached_result = result
    _cached_at = now
    return result


def perform_git_update(project_root: Path) -> UpdateApplyResult:
    """
    Safely fast-forward a Git clone (`git pull --ff-only`) and refresh Python dependencies.

    Refuses to run if tracked files have uncommitted local modifications so user edits are
    never overwritten. Personal `config.toml`, `.venv/`, and `tools/` are gitignored and safe.
    """
    git_bin = shutil.which("git")
    if not git_bin:
        return UpdateApplyResult(
            success=False,
            message="Git is not installed on PATH. Download the latest release manually from GitHub.",
        )

    if not (project_root / ".git").exists():
        return UpdateApplyResult(
            success=False,
            message=(
                "This folder was not installed via `git clone` (no `.git` directory found).\n"
                f"Download the latest release from {RELEASES_WEB_URL}."
            ),
        )

    status_proc = subprocess.run(
        [git_bin, "status", "--porcelain", "--untracked-files=no"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if status_proc.returncode != 0:
        return UpdateApplyResult(
            success=False,
            message=f"`git status` failed: {(status_proc.stderr or status_proc.stdout).strip()}",
        )
    if status_proc.stdout.strip():
        return UpdateApplyResult(
            success=False,
            message=(
                "Uncommitted local edits detected in tracked files.\n"
                "Commit or stash your changes before running one-click update."
            ),
        )

    pull_proc = subprocess.run(
        [git_bin, "pull", "--ff-only"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if pull_proc.returncode != 0:
        err = (pull_proc.stderr or pull_proc.stdout).strip()
        return UpdateApplyResult(
            success=False,
            message=f"`git pull --ff-only` failed:\n{err}",
        )

    pull_out = (pull_proc.stdout or "").strip()
    if "already up to date" in pull_out.lower():
        return UpdateApplyResult(
            success=True,
            message="Already up to date on your current Git branch.",
            needs_restart=False,
        )

    # Refresh Python dependencies silently into the active interpreter
    for req_name in ("requirements.txt", "requirements-gui.txt"):
        req_path = project_root / req_name
        if not req_path.is_file():
            continue
        pip_proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_path)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if pip_proc.returncode != 0:
            err = (pip_proc.stderr or pip_proc.stdout).strip()
            return UpdateApplyResult(
                success=False,
                message=f"Code updated, but `pip install -r {req_name}` failed:\n{err}",
                needs_restart=True,
            )

    return UpdateApplyResult(
        success=True,
        message=f"Update applied cleanly!\n\n{pull_out}\n\nRestart Völundr to load the new version.",
        needs_restart=True,
    )
