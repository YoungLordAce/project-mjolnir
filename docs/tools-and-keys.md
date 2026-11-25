# External Tools & Console Keys

Völundr automatically resolves CLI tools from both your system `PATH` and the local `./tools/` directory. You only need to install the tools for the consoles you actually ingest.

!!! tip "Automatic Download"
    Running `python setup_volundr.py` can download and place these official upstream tools into `./tools/` automatically (skipping anything you already have installed).

---

## External Tools Table

| Tool | Needed For (ROMs / Formats) | Official Download | Where to Place It |
| :--- | :--- | :--- | :--- |
| **7-Zip** (`7z` / `7zz`) | Extracting & peeking `.7z`, `.rar`, `.zip` archives *(recommended for all systems)* | [7-zip.org](https://www.7-zip.org/download.html) / [ip7z/7zip Releases](https://github.com/ip7z/7zip/releases) | **Win:** `tools/7z/7z.exe` *(or standard `C:\Program Files\7-Zip\` install)*<br>**Linux:** `tools/7z/7zz` *(recommended for `.rar`)* or `PATH` |
| **chdman** | **PS1** (single-disc), **PS2**, **PSP**, **Saturn**, **Dreamcast**, **Sega CD**, **PC Engine CD** → `.chd` | [MAME GitHub Releases](https://github.com/mamedev/mame/releases) *(extract `chdman.exe`)* | **Win:** `tools/chdman/chdman.exe`<br>**Linux:** install `mame-tools` on `PATH` |
| **PSXPackager** | **PS1 (multi-disc)** → single `.pbp` with built-in disc switching | [RupertAvery/PSXPackager Releases](https://github.com/RupertAvery/PSXPackager/releases) | **Win:** `tools/psxpackager/psxpackager.exe` (+ `Resources/`)<br>**Linux:** `tools/psxpackager/psxpackager` *(needs `.NET 10` runtime)* |
| **DolphinTool** | **GameCube** & **Wii** → `.rvz` | [Dolphin Downloads](https://dolphin-emu.org/download/) *(included in release `.7z`)* | **Win:** `tools/dolphin/DolphinTool.exe` *(or Dolphin on `PATH`)*<br>**Linux:** install `dolphin-emu-tool` on `PATH` |
| **NSC_Builder** (`squirrel`) / **nsz** | **Nintendo Switch** → merged `.xci` (Base + Update + DLC) & `.nsz`/`.xcz` decompression | [julesontheroad/NSC_BUILDER](https://github.com/julesontheroad/NSC_BUILDER) / [nicoboss/nsz](https://github.com/nicoboss/nsz) | **Win:** `tools/nscb/ztools/squirrel.exe`<br>**Linux:** `tools/nscb/ztools/squirrel.py` or `pip install nsz` |
| **ZArchive** *(optional)* | **Wii U** Loadiine folders → `.wua` *(falls back to clean folders if absent)* | [Exzap/ZArchive Releases](https://github.com/Exzap/ZArchive/releases) | **Win:** `tools/wiiu/zarchive.exe`<br>**Linux:** `tools/wiiu/zarchive` or `PATH` *(pre-built in Docker)* |
| **CDecrypt** *(optional)* | **Wii U** encrypted WUP/NUS (`title.tmd`) decryption | [VitaSmith/cdecrypt Releases](https://github.com/VitaSmith/cdecrypt/releases) | **Win:** `tools/wiiu/cdecrypt.exe`<br>**Linux:** `tools/wiiu/cdecrypt` or `PATH` *(pre-built in Docker)* |
| **pkg2zip** *(optional)* | **PS Vita** Sony `.pkg` → Vita3K-installable `.zip` | [lusid1/pkg2zip Releases](https://github.com/lusid1/pkg2zip/releases) | **Win:** `tools/psvita/pkg2zip.exe`<br>**Linux:** `tools/psvita/pkg2zip` or `PATH` *(pre-built in Docker)* |

*Cartridge systems (**NES, SNES, N64, GBA, NDS**) and **Arcade** sets (**CPS1/2/3, Neo Geo, FBNeo**) work out of the box — just 7-Zip if they arrive inside `.7z`/`.rar` archives.*

---

## User-Supplied Console Keys & Databases

Everything inside `tools/` (except `tools/README.md`) is ignored by `.gitignore` and `.dockerignore`. Supply your own files dumped from hardware you own:

| System | Files | Where to Place Them | What It Enables |
| :--- | :--- | :--- | :--- |
| **Nintendo Switch** | `prod.keys` *(or `keys.txt`)* | `tools/nscb/ztools/` *(or `tools/nscb/`, `tools/switch/`, or `~/.switch/prod.keys`)* | Required by `squirrel` for Switch `.xci` merging & `.nsz` decompression. Völundr automatically copies `prod.keys` → `keys.txt` and refreshes it whenever `prod.keys` is updated. |
| **Nintendo 3DS** | `aes_keys.txt` or `boot9.bin` | `tools/3ds/` *(also checks Azahar/Citra `sysdata/` folders)* | Auto-decrypts encrypted `.3ds` cartridge dumps to `.cci` for Azahar *(ZeroKey dumps decrypt automatically without external key files)*. |
| **PlayStation Vita** | `PSV_GAMES.tsv`, `PSV_DLCS.tsv`, `PSV_UPDATES.tsv` | `tools/psvita/` | Optional automatic `zRIF` lookup, `work.bin` license injection into Vita3K `.zip` archives, and official game title normalization. |

---

## Expected `tools/` Directory Layout

```text
tools/
├── 7z/
│   ├── 7z.exe                  # Windows (optional if installed in C:\Program Files\7-Zip\)
│   └── 7zz                     # Linux standalone binary (enables full .rar support)
├── chdman/
│   └── chdman.exe              # Windows (Linux: install `mame-tools`)
├── dolphin/
│   └── DolphinTool.exe         # Windows (Linux: install `dolphin-emu-tool`)
├── psxpackager/
│   ├── psxpackager.exe         # Windows (+ Resources/ folder)
│   └── psxpackager             # Linux x64 CLI (requires dotnet-runtime-10.0)
├── nscb/
│   └── ztools/
│       ├── squirrel.exe        # Windows (or squirrel.py on Linux)
│       └── prod.keys           # User-supplied Switch keys (`prod.keys` or `keys.txt` — auto-synced)
├── 3ds/
│   ├── aes_keys.txt            # User-supplied 3DS keys (or boot9.bin)
│   └── boot9.bin
├── wiiu/
│   ├── zarchive.exe            # Windows (`zarchive` on Linux; pre-installed in Docker)
│   └── cdecrypt.exe            # Optional: Windows (`cdecrypt` on Linux; pre-installed in Docker)
└── psvita/
    ├── pkg2zip.exe             # Optional: Windows (`pkg2zip` on Linux; pre-installed in Docker)
    ├── PSV_GAMES.tsv           # Optional: user-supplied Vita license/title DB
    ├── PSV_DLCS.tsv
    └── PSV_UPDATES.tsv
```

Run `python main.py doctor` after placing tools to confirm Völundr detects everything cleanly.
