"""Unified external binary runner, streaming subprocess monitor, and console helpers."""

from __future__ import annotations

from collections import deque
import functools
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable


class SubprocessExecutionError(RuntimeError):
    """Raised when an external CLI binary fails to launch, times out, or exits non-zero."""


_ACTIVE_PROCS_LOCK = threading.Lock()
_ACTIVE_PROCS: set[subprocess.Popen[object]] = set()


def _register_proc(proc: subprocess.Popen[object]) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.add(proc)


def _unregister_proc(proc: subprocess.Popen[object]) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.discard(proc)


def _terminate_proc(proc: subprocess.Popen[object], grace_seconds: float = 2.0) -> None:
    """Terminate a running child process cleanly (`terminate` -> `kill` -> reap)."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        proc.kill()
        proc.wait(timeout=5.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def terminate_active_subprocesses(grace_seconds: float = 2.0) -> None:
    """Terminate all active external CLI subprocesses launched through `runner.py` (used on `SIGTERM`)."""
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        _terminate_proc(proc, grace_seconds=grace_seconds)


@functools.lru_cache(maxsize=8)
def has_module(module_name: str) -> bool:
    """Return True if `module_name` is importable in the active Python environment."""
    return importlib.util.find_spec(module_name) is not None


def clear_module_cache() -> None:
    """Invalidate Python's import finder caches and `has_module` LRU cache (e.g. after runtime pip install)."""
    importlib.invalidate_caches()
    has_module.cache_clear()


def has_rich() -> bool:
    """Return True if `rich` is installed."""
    return has_module("rich")


def _stdout_is_tty() -> bool:
    """Return True if stdout is an interactive TTY (safely False under windowless `pythonw.exe`)."""
    stream = sys.stdout
    return bool(stream is not None and hasattr(stream, "isatty") and stream.isatty())


def _win_no_window_flags() -> int:
    """Return `CREATE_NO_WINDOW` on Windows so GUI/pythonw subprocesses never flash console windows."""
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def run_capture(
    cmd: list[str],
    *,
    timeout: float | None = 120.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """
    Execute a short-lived external CLI command (`7z l`, `7z x`, `7z i`, `chdman extractraw`, `gio mount`),
    capturing stdout/stderr with UTF-8 decoding, timeout enforcement, and clean termination on abort.
    """
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(cwd) if cwd is not None else None,
        "env": env,
    }
    if text:
        popen_kwargs["text"] = True
        popen_kwargs["encoding"] = "utf-8"
        popen_kwargs["errors"] = "replace"
    win_flags = _win_no_window_flags()
    if win_flags:
        popen_kwargs["creationflags"] = win_flags

    proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[call-overload]
    _register_proc(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_proc(proc)
        try:
            proc.communicate(timeout=5.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise
    except BaseException:
        _terminate_proc(proc)
        raise
    finally:
        _unregister_proc(proc)

    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def stream_stdout_slice(
    cmd: list[str],
    dest_path: Path,
    max_bytes: int,
    *,
    chunk_size: int = 262144,
    wait_timeout: float = 5.0,
) -> None:
    """
    Stream up to `max_bytes` from `cmd`'s stdout into `dest_path`, then terminate the process cleanly.
    Used for fast header-only peeking (`7z e -so`) without extracting multi-GB files.
    """
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
    }
    win_flags = _win_no_window_flags()
    if win_flags:
        popen_kwargs["creationflags"] = win_flags
    proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[call-overload]
    _register_proc(proc)
    try:
        assert proc.stdout is not None
        with dest_path.open("wb") as dst:
            remaining = max_bytes
            while remaining > 0:
                chunk = proc.stdout.read(min(chunk_size, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                remaining -= len(chunk)
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=wait_timeout)
        finally:
            _unregister_proc(proc)


def run_streaming(
    cmd: list[str],
    status_label: str,
    cwd: Path | None = None,
    abort_substrings: tuple[str, ...] = (),
    error_cls: type[Exception] = SubprocessExecutionError,
) -> None:
    """
    Execute a conversion subprocess while streaming live status updates (`\\r` & `\\n`)
    via `rich` without flooding the terminal. Raises `error_cls` on non-zero exit
    or if any fatal `abort_substrings` (such as `chdman` `nan% complete`) appear.
    """
    tail_lines: deque[str] = deque(maxlen=25)
    use_rich_live = has_rich() and _stdout_is_tty()
    aborted_reason: list[str] = []

    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd) if cwd else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    win_flags = _win_no_window_flags()
    if win_flags:
        popen_kwargs["creationflags"] = win_flags

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[call-overload]
    except OSError as exc:
        raise error_cls(f"Failed launching `{cmd[0]}`: {exc}") from exc

    _register_proc(proc)

    def _check_abort(line: str) -> bool:
        for bad in abort_substrings:
            if bad in line:
                aborted_reason.append(line)
                proc.kill()
                return True
        return False

    def _consume_stream(update_cb: Callable[[str], None]) -> None:
        assert proc.stdout is not None
        buf = b""
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            while True:
                r_idx = buf.find(b"\r")
                n_idx = buf.find(b"\n")
                if r_idx == -1 and n_idx == -1:
                    break
                if r_idx != -1 and (n_idx == -1 or r_idx < n_idx):
                    sep = r_idx
                    step = 1
                else:
                    sep = n_idx
                    step = 1
                line_bytes = buf[:sep]
                buf = buf[sep + step :]
                text = line_bytes.decode("utf-8", errors="replace").strip()
                if text:
                    tail_lines.append(text)
                    update_cb(text)
                    if _check_abort(text):
                        return
        if buf:
            rem = buf.decode("utf-8", errors="replace").strip()
            if rem:
                tail_lines.append(rem)
                update_cb(rem)
                _check_abort(rem)

    interrupted = False
    try:
        if use_rich_live:
            from rich.console import Console

            console = Console()
            with console.status(f"[bold cyan]{status_label}[/bold cyan]", spinner="dots") as status:
                def _on_line(line: str) -> None:
                    clipped = line[:90] + ("..." if len(line) > 90 else "")
                    status.update(f"[bold cyan]{status_label}[/bold cyan] [dim]— {clipped}[/dim]")

                _consume_stream(_on_line)
        else:
            _consume_stream(lambda _line: None)
    except BaseException:
        interrupted = True
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if (interrupted or aborted_reason) and proc.poll() is None:
            _terminate_proc(proc)
        ret = proc.wait()
        _unregister_proc(proc)

    if aborted_reason:
        raise error_cls(
            f"`{Path(cmd[0]).name}` aborted during {status_label} (detected invalid zero-track stream: "
            f"{aborted_reason[0]!r})"
        )

    if ret != 0:
        recent = "\n  ".join(tail_lines) if tail_lines else "(no output)"
        raise error_cls(
            f"`{Path(cmd[0]).name}` failed with exit code {ret} during {status_label}:\n  {recent}"
        )


def run_psxpackager(
    cmd: list[str],
    work_dir: Path,
    status_label: str,
    error_cls: type[Exception] = SubprocessExecutionError,
) -> deque[str]:
    """
    Execute `psxpackager` safely across Windows 11 and Fedora Linux:
      - Sets `DOTNET_ROLL_FORWARD=LatestMajor` so .NET 6/7 CLI builds run on .NET 8+.
      - Redirects `TEMP`/`TMP`/`TMPDIR` into `work_dir/_tmp` inside `.staging/` so multi-track
        disc merges never fill Linux `/tmp` RAM tmpfs or leak outside staging.
      - On Windows, allocates a hidden console buffer (`CREATE_NEW_CONSOLE` + `SW_HIDE`) so
        `PSXPackager`'s `Console.CursorVisible` never fails with `Invalid handle` when piped,
        while live-tailing `psxpackager`'s `-g` log file in `work_dir` for `rich` progress.
      - On Linux/POSIX, redirects `stderr` to a file inside `work_dir` instead of an unread pipe
        so verbose `.NET` diagnostics can never fill the 64 KiB pipe buffer and deadlock.
    """
    tmp_dir = work_dir / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["DOTNET_ROLL_FORWARD"] = "LatestMajor"
    tmp_str = str(tmp_dir.resolve())
    env.setdefault("DOTNET_BUNDLE_EXTRACT_BASE_DIR", tmp_str)
    env["TEMP"] = tmp_str
    env["TMP"] = tmp_str
    env["TMPDIR"] = tmp_str

    tail_lines: deque[str] = deque(maxlen=30)
    log_offsets: dict[Path, int] = {}
    use_rich_live = has_rich() and _stdout_is_tty()

    def _drain_logs(update_cb: Callable[[str], None]) -> None:
        for log_file in sorted(work_dir.glob("*.log")):
            prev = log_offsets.get(log_file, 0)
            try:
                with log_file.open("rb") as fh:
                    fh.seek(prev)
                    chunk = fh.read()
                    log_offsets[log_file] = fh.tell()
            except OSError:
                continue
            if not chunk:
                continue
            for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if line:
                    tail_lines.append(line)
                    update_cb(line)

    popen_kwargs: dict[str, object] = {
        "cwd": str(work_dir.resolve()),
        "env": env,
    }
    stderr_file_path: Path | None = None
    stderr_fh = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        popen_kwargs["startupinfo"] = startupinfo
    else:
        stderr_file_path = work_dir / "_psxpackager_stderr.txt"
        stderr_fh = stderr_file_path.open("wb")
        popen_kwargs["stdout"] = subprocess.DEVNULL
        popen_kwargs["stderr"] = stderr_fh

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        if stderr_fh is not None:
            stderr_fh.close()
        raise error_cls(f"Failed launching `{cmd[0]}`: {exc}") from exc

    _register_proc(proc)
    stderr_text = ""
    interrupted = False
    try:
        if use_rich_live:
            from rich.console import Console

            console = Console()
            with console.status(f"[bold cyan]{status_label}[/bold cyan]", spinner="dots") as status:
                def _on_line(line: str) -> None:
                    clipped = line[:90] + ("..." if len(line) > 90 else "")
                    status.update(f"[bold cyan]{status_label}[/bold cyan] [dim]— {clipped}[/dim]")

                while proc.poll() is None:
                    _drain_logs(_on_line)
                    time.sleep(0.15)
                _drain_logs(_on_line)
        else:
            while proc.poll() is None:
                _drain_logs(lambda _line: None)
                time.sleep(0.10)
            _drain_logs(lambda _line: None)
    except BaseException:
        interrupted = True
        raise
    finally:
        if interrupted and proc.poll() is None:
            _terminate_proc(proc)
        ret = proc.wait()
        _unregister_proc(proc)
        if stderr_fh is not None:
            try:
                stderr_fh.close()
            except OSError:
                pass
        if stderr_file_path is not None and stderr_file_path.is_file():
            try:
                stderr_text = stderr_file_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                stderr_text = ""

    if stderr_text:
        for sline in stderr_text.splitlines():
            if sline.strip():
                tail_lines.append(sline.strip())

    if ret != 0:
        recent = "\n  ".join(tail_lines) if tail_lines else "(no log output)"
        raise error_cls(
            f"`{Path(cmd[0]).name}` failed with exit code {ret} during {status_label}:\n  {recent}"
        )

    return tail_lines
