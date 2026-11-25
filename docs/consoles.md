# Supported Consoles & Conversion Rules

Project Mjolnir uses binary header inspection, ISO9660 volume parsing, and lightweight 16 MiB archive previews to classify ROMs accurately before extracting or converting.

---

## Arcade (5-Way FBNeo Split)
- Peeks inside `.zip`/`.7z`/`.rar` archives in memory **without extracting them**.
- Routes recognized arcade sets untouched (`archive_policy = "passthrough"`) into `cps1/`, `cps2/`, `cps3/`, `neogeo/`, or `fbneo/`.

## PlayStation 1 (`psx`)
- **Single-disc games:** Converted to `.chd` via `chdman createcd`.
- **Multi-disc sets:** Merged into a single `<Title>.pbp` via `PSXPackager` (`psxpackager`) so standalone DuckStation and frontends like Cocoon FE launch cleanly with built-in disc switching (avoiding Android SAF `.m3u` errors), and cleans up any legacy `.m3u` / `_discs/` files in the destination.
- **LibCrypt `.sbi` sidecars:** Automatically staged into `psx/subchannels/`.
- **CUE Healing:** Repairs single- and multi-track `.cue` sheets with single quotes or watermarked track filenames.

## PlayStation 2, PSP, Sega & PC Engine CD
- **Sega CD (`segacd`), Saturn (`saturn`), Dreamcast (`dreamcast`), PC Engine CD (`pcenginecd`), and `<750 MiB` PS2 CDs:** Converted via `chdman createcd` (`.chd`).
- **PS2 DVDs (`ps2`):** Converted via `chdman createdvd` (`.chd`).
- **PSP UMDs (`psp`):** Converted via `chdman createdvd -hs 2048` (PPSSPP optimal 2 KiB hunk size).
- **Multi-disc playlists:** Non-PS1 multi-disc sets (`gc`, `dreamcast`, `saturn`, `ps2`, etc.) store raw `(Disc 1..N)` files inside `<system>/_discs/` with a single `<Title>.m3u` playlist in `<system>/` using POSIX `/` relative paths.

## GameCube & Wii (`gc`, `wii`)
- Converts `.iso`, `.gcm`, `.wbfs`, and `.ciso` images to `.rvz` via `DolphinTool convert -f rvz -b 131072 -c zstd -l 5`.

## Nintendo Switch (`switch`)
- **TitleID Family Grouping:** Groups Base + Update + DLC by Horizon OS 16-hex TitleID family (also pairs untagged No-Intro `.xci` archives with sibling updates by normalized game title and honors explicit `[BASE]` tags even if an archive has a typo'd Update TitleID).
- **Library Donor Upgrading:** When a newer Update or DLC arrives later for a game already in `library_root/switch/`, Mjolnir pulls the existing `.xci`/`.nsp` Base donor into local `.staging/`, merges with `NSC_Builder` (`squirrel`), routes the upgraded `.xci`, and deletes the superseded file.
- **Duplicate/Older Update Guard:** Update-only drops with a numeric version `<=` the version already merged in the library donor are skipped automatically without copying or rebuilding.
- **`.nsz`/`.xcz` Decompression:** Automatically decompresses compressed Switch packages for emulators requiring uncompressed `.nsp`/`.xci`.

## Nintendo 3DS (`3ds`)
- **Streaming Decryption:** Pure-Python streaming NCSD/NCCH AES-CTR decryption when user-supplied `tools/3ds/aes_keys.txt` or `tools/3ds/boot9.bin` is present (ZeroKey-encrypted dumps decrypt automatically without external key files).
- **Azahar Normalization:** Renames decrypted `.3ds` cartridge dumps to `.cci`; safely quarantines encrypted dumps if required keys are missing.
- **Updates & DLC:** Routes installable `.cia` packages into `3ds/_cia_install/`.

## Nintendo Wii U (`wiiu`)
- **ZArchive `.wua` Compression:** Packs Loadiine (`code/`, `content/`, `meta/`) folders into compressed `.wua` archives via `zarchive` (using `<16-hex-titleId>_v<version>` virtual slots for Cemu).
- **WUP/NUS Decryption:** Automatically decrypts encrypted `title.tmd` + `.app`/`.h3` packages when `cdecrypt` is present (quarantines safely if absent).
- **Clean Fallback & Passthrough:** Falls back to routing clean `{code, content, meta}` folders when `zarchive` is absent (stripping redundant encrypted `.app`/`.h3` blobs from hybrid archives), and routes existing `.wua`/`.wux` files directly.

## PlayStation Vita (`psvita`)
- **Binary `param.sfo` Parsing:** Reads `TITLE_ID`, `APP_VER`, `CATEGORY` (`gd`/`gp`/`ac`), and `TITLE` directly from `sce_sys/param.sfo` (`\x00PSF`).
- **Vita3K `.zip` Repacking:** Repacks unpacked or `.zip`/`.7z`/`.rar` NoNpDrm / MaiDump / Vitamin trees (including multi-package Base + Update + DLC bundles) into clean `<Title> [<TitleID>] [v<Ver>].zip` archives with watermark sidecars stripped.
- **Sony `.pkg` Unpacking:** Unpacks `.pkg` (`\x7fPKG`) + `work.bin`/`.zrif` (or user-supplied `tools/psvita/PSV_*.tsv` lookup) via `pkg2zip`.
- **Frontend Shortcuts & Add-ons:** Generates Cocoon FE `<Title>.dpt` tag shortcuts (`[vita_game_id] <TitleID>`) in `psvita/` and routes standalone Updates/DLC into `psvita/_updates_dlc/`.

## Cartridge Systems (`nes`, `snes`, `n64`, `gba`, `nds`)
- Extracts ROMs from `.zip`/`.7z`/`.rar` (or keeps `.zip` if configured with `archive_policy = "keep_zip"`), strips ROM-site watermarks, and routes cleanly into `<library_root>/<system>/`.
