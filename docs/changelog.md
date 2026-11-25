# Changelog

All notable changes to Völundr are documented here.
Versioning follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).

## [0.2.0] - 2026-11-25

### Added
- **Guided Setup Script (`setup_volundr.py`):** Stdlib-only interactive installer that creates `.venv`, generates `config.toml` with your `library_root`, downloads official upstream CLI tools into `tools/` (with `--dry-run`, `--yes`, and per-tool selection), and runs `main.py doctor`. Made the default Quick Start method in `README.md`.
- **Automatic Switch `prod.keys` → `keys.txt` Sync:** Auto-discovers standard Switch `prod.keys` (in `tools/nscb/ztools/`, `tools/nscb/`, `tools/switch/`, or `~/.switch/prod.keys`) and automatically copies/refreshes it to `keys.txt` for `NSC_Builder` (`squirrel`) without modifying the original `prod.keys`.
- **Duplicate/Older Switch Update Guard:** Update-only Switch drops with a numeric version `<=` the version already merged in the library donor now skip copying and rebuilding, saving multi-GB network/disk I/O while safely cleaning up the dropped file.
- **Zensical Documentation Site:** Added `zensical.toml`, `docs/`, and `.github/workflows/docs.yml` for automated GitHub Pages deployment.

### Fixed
- **Untagged No-Intro Switch `.xci` Pairing in `watch` Mode:** Pairs untagged Switch Base archives with sibling TitleID-bearing Updates/DLCs in `drop/` by normalized game title and passes `game_title_hint` when checking the library for an untagged Base donor.
- **Mis-Tagged Switch `[BASE]` Filenames:** Recognizes explicit `[BASE]` / `(BASE)` tags even when an archive filename accidentally carries an Update TitleID (`...800`).
- **Colons in Zip Entry Names:** Tightened Zip-Slip validation (`validate_safe_entries`) to block real Windows drive prefixes (`C:...`) while allowing colons inside game titles (e.g. `The Legend of Zelda: Breath of the Wild`), and sanitized Windows-illegal characters in stdlib zip preview/fallback paths.
- **Terminal Version Tag Display:** Escaped Rich markup in `watch` logs and routing summaries so bracketed version tags like `[v262144]` render verbatim.

## [0.1.0] - 2026-11-23

### Added
- Initial release of Völundr: cross-platform automated ROM ingestion pipeline for Windows, Linux, and Docker (`doctor`, `inspect`, `process`, `scan`, `watch`).
- Zero-extraction 5-way arcade classification (`cps1`, `cps2`, `cps3`, `neogeo`, `fbneo`) and binary header / ISO9660 disc sniffing.
- Format conversion & routing for PS1 (`.chd` / multi-disc `.pbp` + `.sbi`), PS2/PSP/Sega/PCE CD (`.chd`), GameCube/Wii (`.rvz`), Nintendo Switch (merged `.xci` + `.nsz` decompression), 3DS (streaming AES-CTR decryption + `.cci` + `_cia_install`), Wii U (`.wua` / `cdecrypt` / Loadiine fallback), and PS Vita (`param.sfo`, Vita3K `.zip`, `.pkg` + `pkg2zip`, Cocoon FE `.dpt` tags).
- Syncthing- and Samba-safe atomic transfers (`~syncthing~*.tmp` → byte verification → `os.replace()`), ROMarr two-library platform bucket support, example TOML templates, friendly missing-config guidance, and MIT License.
