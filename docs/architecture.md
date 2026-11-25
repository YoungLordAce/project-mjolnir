# Architecture & Testing

## Module Overview (`src/`)

| Module | Responsibility |
| :--- | :--- |
| `src/constants.py` | Archive extensions, ignored sidecar/metadata sets, temp download markers, Syncthing temp prefixes, default key paths, and ROMarr platform bucket names |
| `src/runner.py` | Unified external process execution (`run_capture`, `run_streaming`, `run_psxpackager`, `stream_stdout_slice`) with `rich` spinners, tail error ring-buffers, and Linux `.NET` bundle environment handling |
| `src/config_loader.py` | TOML configuration parser, friendly missing-config guidance (`MissingConfigError`), cross-platform binary/key resolver, cached `7z` RAR capability probe, and Linux GNOME GVFS `smb://` mount resolver |
| `src/archive_handler.py` | Cached zero-extraction archive listing, 5-way arcade classification (`arcade_db.py`), lock-protected `StagingJobDir` context manager, Zip-Slip validation, and 16 MiB header-preview streaming |
| `src/identifier.py` | 6-pass directory & binary header sniffer (Wii U Loadiine & WUP/NUS trees, PS Vita `sce_sys/param.sfo` & `.pkg` + TSV lookup, Switch family bundles, `.cue`/`.gdi` optical descriptors, and standalone magic-byte/ISO9660 inspection) |
| `src/ctr_decrypt.py` | Pure-Python streaming 3DS NCSD/NCCH AES-CTR decryptor and `inspect_3ds_header()` (`NoCrypto` vs `ZeroKey` vs keyslot `0x2C`/`0x25`/`0x18`/`0x1B`) |
| `src/switch_bundler.py` | Horizon OS TitleID (`16-hex`) family grouping, PFS0 `.tik` inspection, duplicate/older update detection, explicit `[BASE]` / add-on tag classification, and library Base donor discovery backed by bounded `@lru_cache(maxsize=4096)` |
| `src/converter.py` | Unified `ConversionStrategy` registry (`CONVERSION_STRATEGIES`) driving live conversion, `--dry-run` prediction, `inspect` planning, Switch `prod.keys` → `keys.txt` sync, CUE healing, and PSX `.sbi` sidecar staging |
| `src/router.py` | Syncthing-safe atomic file & directory transfers (`~syncthing~<name>.tmp` → byte verification → `os.replace()`), POSIX `.m3u` generation, quarantine routing, and batch orchestration |
| `src/watcher.py` | Watchdog event listener, Win32 `CreateFileW(dwShareMode=0)` / POSIX `flock` lock verifier, ROMarr platform-bucket expansion, multi-disc & Switch family coalescing, and sequential worker queue |

---

## Running the Unit Test Suite

All tests run self-contained against `config.example.toml` and `config.docker.example.toml` (no personal `config.toml` or network access required):

```bash
# Windows
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Linux / macOS
./.venv/bin/python -m unittest discover -s tests -v
```

---

## Building This Docs Site Locally

To preview the Zensical documentation locally before pushing:

```bash
pip install zensical
zensical serve
```

GitHub Actions (`.github/workflows/docs.yml`) automatically runs `zensical build --clean` and publishes to GitHub Pages on every push to `main`.
