# External Tools & Keys (`tools/`)

Everything inside `tools/` except this file is ignored by `.gitignore` — no third-party binaries, decryption keys, or license databases are tracked in Git.

See the **[External Tools Table in `README.md`](../README.md#external-tools)** for official download links and which consoles use each tool, and **[`DOCUMENTATION.md`](../DOCUMENTATION.md)** for Linux package manager & Docker details.

## Expected Directory Layout

Only populate the folders for the consoles you actually ingest (tools already on your system `PATH` do not need to be copied here):

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

Run `python main.py doctor` after placing tools to confirm Mjolnir detects them cleanly.
