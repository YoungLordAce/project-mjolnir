# Configuration Reference

Völundr keeps personal configuration files (`config.toml` and `config.docker.toml`) out of Git and ships cleanly documented templates:

- **`config.example.toml`** — Desktop/workstation template (Windows & Linux profiles).
- **`config.docker.example.toml`** — Containerized Linux/NAS template (`/app/drop` → `/library`).

Relative paths in `config.toml` are always resolved relative to the directory containing `config.toml`, so `python main.py` can be invoked from any working directory.

---

## Minimum Fields to Edit

For a first-time desktop setup, you only need to set **`library_root`** for your OS (which `python setup_volundr.py` can do interactively for you):

```toml
[paths.windows]
library_root = 'Z:\games\library\roms'   # Local path, mapped drive, or UNC share

[paths.linux]
library_root = "/mnt/nas/games/library/roms"  # Local/fstab mount or smb://my-nas.local/...
auto_mount_smb = true
```

---

## Key Sections

### `[general]`
| Setting | Default | Description |
| :--- | :--- | :--- |
| `post_process_action` | `"move"` | `"move"` moves originals into `processed_dir` after verified arrival; `"delete"` removes them permanently |
| `stability_poll_seconds` | `2.0` | Seconds between size/mtime stability checks on newly dropped items |
| `stability_checks_required` | `3` | Consecutive identical checks required before queueing a file/folder |
| `min_free_staging_gb` | `5.0` | Minimum free disk space (GiB) required in `staging_dir` before extraction |

### `[paths]`
| Setting | Default | Description |
| :--- | :--- | :--- |
| `watch_dir` | `"./drop"` | Folder monitored by `scan` and `watch` |
| `staging_dir` | `"./.staging"` | Temporary local workspace for extraction & conversion |
| `processed_dir` | `"./processed"` | Destination for completed originals when `post_process_action = "move"` |
| `quarantine_dir` | `"./quarantine"` | Destination for unidentified files or encrypted dumps missing keys |

### `[binaries.windows]` & `[binaries.linux]`
Explicit candidate paths checked alongside your system `PATH` (`shutil.which()`). You rarely need to edit these if tools live on `PATH` or inside `./tools/`. For `squirrel_keys`, Völundr checks both `keys.txt` and `prod.keys` (including `~/.switch/prod.keys`).

### `[conversion]`
Controls per-console defaults such as:
- `psx_multidisc_pbp = true`, `psx_pbp_compression_level = 5`
- `psp_to_chd = true`, `psp_hunk_size = 2048`
- `rvz_block_size = 131072`, `rvz_compression = "zstd"`, `rvz_compression_level = 5`
- `switch_merge_format = "xci"`, `decompress_nsz = true`
- `rename_3ds_to_cci = true`, `cia_subdir = "_cia_install"`
- `generate_m3u = true`, `multidisc_subdir = "_discs"`
- `wiiu_to_wua = true`
- `vita_repack_zip = true`, `vita_addons_subdir = "_updates_dlc"`

### `[systems.<id>]`
Each supported system maps to a destination subfolder inside `library_root` and an `archive_policy`:
- `"passthrough"` — Never extract archive (used for Arcade sets: `cps1`, `cps2`, `cps3`, `neogeo`, `fbneo`).
- `"extract"` — Extract ROM/disc image from archives before identification, conversion, and routing.
- `"keep_zip"` — Keep single-ROM `.zip` files intact while still extracting `.7z`/`.rar`.
