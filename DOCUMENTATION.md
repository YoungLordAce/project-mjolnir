# Project Mjolnir — Technical Documentation

For 5-minute first-time setup and the external tool download table, see [`README.md`](README.md). This document covers per-console pipeline behavior, Linux/Docker/NAS/ROMarr deployment, configuration details, and codebase architecture.

---

## Detailed Pipeline Behavior

### Zero-Extraction Arcade Detection (5-Way FBNeo Split)
- Peeks inside `.zip`/`.7z`/`.rar` archives in memory without extracting them.
- Routes recognized arcade sets untouched (`archive_policy = "passthrough"`) into `cps1/`, `cps2/`, `cps3/`, `neogeo/`, or `fbneo/`.

### Binary Header & ISO9660 Sniffing
- Inspects magic bytes and internal `SYSTEM.CNF` / `PSP_GAME` records across 2048B `.iso`, 2352B Mode 1 / Mode 2 XA `.bin`, `.rvz`, `.ciso`, `.pbp`, and `.chd` containers (`psx`, `ps2`, `psp`, `gc`, `wii`, `saturn`, `dreamcast`, `segacd`, `pcenginecd`, and `3ds` `NCSD`/`NCCH` encryption headers).
- Uses lightweight 16 MiB header previews during archive inspection (creating 0-byte placeholders for secondary CD-DA tracks, non-metadata Wii U files, and non-`param.sfo` PS Vita files) so large disc archives inspect quickly without full extraction.

### Per-Console Conversion Rules
- **PlayStation 1 (`psx`):**
  - Single-disc games convert to `.chd` via `chdman createcd`.
  - Multi-disc sets merge into a single `<Title>.pbp` via `PSXPackager` (`psxpackager`) so standalone DuckStation and frontends like Cocoon FE launch cleanly with built-in disc switching (avoiding Android SAF `.m3u` errors), and clean up any legacy `.m3u` / `_discs/` files in the destination.
  - LibCrypt `.sbi` sidecars are automatically staged into `psx/subchannels/`.
- **PlayStation 2 (`ps2`), PSP (`psp`), Sega & PC Engine CD (`saturn`, `dreamcast`, `segacd`, `pcenginecd`):**
  - Sega CDs and `<750 MiB` PS2 CDs convert via `chdman createcd` (`.chd`).
  - PS2 DVDs convert via `chdman createdvd` (`.chd`).
  - PSP UMDs convert via `chdman createdvd -hs 2048` (PPSSPP optimal 2 KiB hunk size).
- **Multi-Disc Playlists & CUE Healing:**
  - Repairs single- and multi-track `.cue` sheets with single quotes or watermarked track filenames.
  - Stores non-PS1 multi-disc sets (`gc`, `dreamcast`, `saturn`, `ps2`, etc.) inside `<system>/_discs/` with a single `<Title>.m3u` playlist in `<system>/` using POSIX `/` relative paths (hiding duplicate disc entries from top-level frontend scanners while avoiding Samba/Android dotfile `Hidden` attributes).
- **GameCube & Wii (`gc`, `wii`):**
  - Converts `.iso`/`.gcm`/`.wbfs`/`.ciso` to `.rvz` via `DolphinTool convert -f rvz -b 131072 -c zstd -l 5`.
- **Nintendo Switch (`switch`):**
  - Groups Base + Update + DLC by Horizon OS 16-hex TitleID family (including pulling an existing Base donor from `library_root/switch/` when an update or DLC arrives separately, backed by bounded `@lru_cache(maxsize=4096)` keyed on `(path, mtime_ns, size)`).
  - Merges multi-file sets into a single `.xci` (or `.nsp`) via `NSC_Builder` (`squirrel`) and decompresses `.nsz`/`.xcz` for emulators that require uncompressed images.
- **Nintendo 3DS (`3ds`):**
  - Pure-Python streaming NCSD/NCCH AES-CTR decryption when user-supplied `tools/3ds/aes_keys.txt` or `tools/3ds/boot9.bin` is present (ZeroKey-encrypted dumps are decrypted automatically even without external key files).
  - Normalizes decrypted cartridge dumps from `.3ds` -> `.cci` for Azahar; safely quarantines encrypted dumps if required keys are missing.
  - Routes installable `.cia` updates/DLC to `3ds/_cia_install/`.
- **Nintendo Wii U (`wiiu`):**
  - Compresses Loadiine (`code/`, `content/`, `meta/`) folders into `.wua` ZArchives via `zarchive` (with `<16-hex-titleId>_v<version>` virtual directory slotting for Cemu).
  - Automatically decrypts encrypted WUP/NUS (`title.tmd` + `.app`/`.h3`) dumps when `cdecrypt` is present (quarantines safely if absent).
  - Falls back to routing clean `{code, content, meta}` Loadiine folders when `zarchive` is absent, stripping redundant encrypted `.app`/`.h3` blobs from hybrid archives.
  - Passes existing `.wua`/`.wux` files through directly.
- **PlayStation Vita (`psvita`):**
  - Parses binary `sce_sys/param.sfo` (`\x00PSF`) for `TITLE_ID`, `APP_VER`, `CATEGORY` (`gd`/`gp`/`ac`), and `TITLE`.
  - Repacks unpacked or `.zip`/`.7z`/`.rar` NoNpDrm / MaiDump / Vitamin trees (including multi-package Base + Update + DLC bundles) into Vita3K-installable `<Title> [<TitleID>] [v<Ver>].zip` archives with watermark sidecars stripped.
  - Unpacks Sony `.pkg` (`\x7fPKG`) + `work.bin`/`.zrif` (or user-supplied `tools/psvita/PSV_*.tsv` lookup) via `pkg2zip`.
  - Generates companion Cocoon FE `<Title>.dpt` tag shortcuts (`[vita_game_id] <TitleID>`) in `psvita/` and routes standalone Updates/DLC into `psvita/_updates_dlc/`.

### Syncthing & Samba-Safe Atomic Routing
- Stages all extractions and conversions locally in `.staging/` inside lock-protected `StagingJobDir` contexts.
- Streams outputs to `~syncthing~<name>.tmp` at the destination (preventing partial Syncthing transfers and avoiding Samba leading-dot `Hidden` attributes), verifies final byte sizes, and commits atomically with `os.replace()`.
- Watches both top-level drops and ROMarr platform-bucket subdirectories (`drop/<platform>/<file>`).

---

## Linux Setup Notes

### Package Installation (Example: Fedora)
```bash
sudo dnf install 7zip mame-tools dolphin-emu-tool dotnet-runtime-10.0
```
- `7zip` provides `7z`, `mame-tools` provides `chdman`, `dolphin-emu-tool` provides `dolphin-tool`, and `dotnet-runtime-10.0` runs the Linux x64 `psxpackager` CLI placed in `tools/psxpackager/psxpackager` (Mjolnir sets `DOTNET_ROLL_FORWARD=LatestMajor` automatically).

### Full `.rar` Support on Linux (`7zz`)
Some Linux distro `7zip` packages (`/usr/bin/7z`) omit proprietary RAR codecs due to licensing guidelines. Download the official upstream standalone `7zz` executable into `tools/7z/7zz`:
```bash
mkdir -p tools/7z
curl -fL -o /tmp/7z.tar.xz https://github.com/ip7z/7zip/releases/download/26.06/7z2606-linux-x64.tar.xz
tar -xf /tmp/7z.tar.xz -C tools/7z 7zz
chmod +x tools/7z/7zz
```
Mjolnir probes `7z i` (cached) and automatically prioritizes a binary with RAR support over `/usr/bin/7z`.

### GNOME GVFS `smb://` Auto-Mount
On Linux desktops using GNOME Files, `[paths.linux] -> library_root` accepts either a standard mount path (`/mnt/nas/games/library/roms`) or a direct SMB URI (`smb://my-nas.local/games/library/roms`). When `auto_mount_smb = true`, Mjolnir resolves `/run/user/$UID/gvfs/` and runs `gio mount` automatically using saved GNOME Keyring credentials if the share is not yet mounted.

---

## Docker Deployment (24/7 Headless NAS + ROMarr)

Project Mjolnir can run headless in Docker on a NAS or home server via [`Dockerfile`](Dockerfile) and [`config.docker.example.toml`](config.docker.example.toml) (copied to `config.docker.toml`, leaving `config.toml` untouched for desktop machines).

### What the Multi-Stage Image Includes
- Fedora runtime + Python 3.11 + `7zz` (with RAR/RAR5 support)
- `chdman` (`mame-tools`) + `dolphin-tool` (`dolphin-emu-tool`)
- Compiled Linux x64 `zarchive`, `cdecrypt`, and `pkg2zip` in `/usr/local/bin/`
- `dotnet-runtime-10.0` (for optional host-mounted `psxpackager`)
- `NSC_Builder` (`squirrel.py` in `/opt/nscb/`) + `nsz` + `pycryptodome` + `pyctr`

### Recommended Container Volume Mounts
In your `docker-compose.yml`, bind-mount your host paths to the defaults in `config.docker.example.toml`:
- `/app/drop` — watched intake folder
- `/library` — destination ROM library (`library_root`)
- `/app/processed` — completed originals (when `post_process_action = "move"`)
- `/app/quarantine` — unidentified or encrypted items needing attention
- `/app/tools` *(read-only)* — optional host folder containing `tools/psxpackager/`, `tools/3ds/`, `tools/nscb/ztools/keys.txt`, and/or `tools/psvita/*.tsv`

### ROMarr Two-Library Integration
Set ROMarr's default intake library to your Mjolnir `drop/` mount (e.g., `/path/to/mjolnir/drop`) and add your destination ROM library (`/path/to/library/roms`) as a second catalog library so ROMarr continues tracking converted `.chd`/`.rvz`/`.pbp`/`.xci`/`.cci`/`.wua`/`.zip` files after Mjolnir processes `drop/`.

### Example Stack Commands
```bash
cp config.docker.example.toml config.docker.toml
cd /path/to/stacks/mjolnir
docker compose up -d --build
docker exec mjolnir /app/.venv/bin/python /app/main.py --config /app/config.docker.toml doctor
docker logs -f mjolnir
```

---

## Configuration Reference

See [`config.example.toml`](config.example.toml) for every option with inline comments. Key settings:
- `general.post_process_action`: `"move"` (moves processed sources into `processed_dir`; safest default) or `"delete"` (removes sources after verified destination write).
- `paths.<os>.library_root`: Destination root containing per-system subfolders (`psx/`, `gc/`, `switch/`, etc.).
- `systems.<id>.archive_policy`:
  - `"passthrough"` — never extract archives (arcade sets).
  - `"extract"` — extract ROM/image before conversion/routing.
  - `"keep_zip"` — keep single-ROM `.zip` intact while still extracting `.7z`/`.rar`.

---

## Architecture Overview (`src/`)

| Module | Responsibility |
| :--- | :--- |
| `src/constants.py` | Archive extensions, ignored sidecar/metadata sets, temp download markers, Syncthing temp prefixes, and ROMarr platform bucket names |
| `src/runner.py` | Unified external process execution (`run_capture`, `run_streaming`, `run_psxpackager`, `stream_stdout_slice`) with `rich` spinners, tail error ring-buffers, and Linux `.NET` bundle environment handling |
| `src/config_loader.py` | TOML configuration parser, friendly missing-config guidance, cross-platform binary/key resolver, cached `7z` RAR capability probe, and Linux GNOME GVFS `smb://` mount resolver |
| `src/archive_handler.py` | Cached zero-extraction archive listing, 5-way arcade classification (`arcade_db.py`), lock-protected `StagingJobDir` context manager, and 16 MiB header-preview streaming |
| `src/identifier.py` | 6-pass directory & binary header sniffer (Wii U Loadiine & WUP/NUS trees, PS Vita `sce_sys/param.sfo` & `.pkg` + TSV lookup, Switch family bundles, `.cue`/`.gdi` optical descriptors, and standalone magic-byte/ISO9660 inspection) |
| `src/ctr_decrypt.py` | Pure-Python streaming 3DS NCSD/NCCH AES-CTR decryptor and `inspect_3ds_header()` (`NoCrypto` vs `ZeroKey` vs keyslot `0x2C`/`0x25`/`0x18`/`0x1B`) |
| `src/switch_bundler.py` | Horizon OS TitleID (`16-hex`) family grouping, PFS0 `.tik` inspection, and library Base donor discovery backed by bounded `@lru_cache(maxsize=4096)` |
| `src/converter.py` | Unified `ConversionStrategy` registry (`CONVERSION_STRATEGIES`) driving live conversion, `--dry-run` prediction, `inspect` planning, CUE healing, and PSX `.sbi` sidecar staging |
| `src/router.py` | Syncthing-safe atomic file & directory transfers (`~syncthing~<name>.tmp` -> byte verification -> `os.replace()`), POSIX `.m3u` generation, quarantine routing, and batch orchestration |
| `src/watcher.py` | Watchdog event listener, Win32 `CreateFileW(dwShareMode=0)` / POSIX `flock` lock verifier, ROMarr platform-bucket expansion, and sequential worker queue |

---

## Running Tests

Tests run self-contained against `config.example.toml` and `config.docker.example.toml` (no personal `config.toml` required):

```bash
# Windows
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Linux / macOS
./.venv/bin/python -m unittest discover -s tests -v
```
