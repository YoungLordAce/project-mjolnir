# Getting Started

## Guided Setup (Recommended — 3 Steps)

Requires **Python 3.11+**.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YoungLordAce/project-mjolnir.git
   cd project-mjolnir
   ```

2. **Run the guided setup script:**
   ```bash
   python setup_mjolnir.py
   ```
   `setup_mjolnir.py` uses only the Python standard library and walks you through:
   - Creating `.venv` and installing `requirements.txt`
   - Copying `config.example.toml` → `config.toml` and setting your `library_root` (never overwrites an existing `config.toml`)
   - Downloading official third-party CLI tools into `./tools/` (**All**, **Choose by console**, or **None**)
   - Running `main.py doctor` to verify your paths, auto-create `./drop`, `./.staging`, `./processed`, and `./quarantine`, and check every detected tool

   !!! tip "Preview Before Installing"
       Run `python setup_mjolnir.py --dry-run` first to preview every step and official download URL without modifying files or downloading anything.

3. **Drop ROMs into `./drop` and start watching:**
   ```bash
   # Windows
   .\.venv\Scripts\python.exe main.py watch

   # Linux / macOS
   ./.venv/bin/python main.py watch
   ```

---

## Manual Setup

If you prefer setting up each piece by hand:

1. **Create a virtual environment and install dependencies:**
   ```bash
   python -m venv .venv

   # Windows (PowerShell)
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt

   # Linux / macOS
   ./.venv/bin/pip install -r requirements.txt
   ```

2. **Create `config.toml`:**
   ```bash
   # Windows (PowerShell)
   Copy-Item config.example.toml config.toml

   # Linux / macOS
   cp config.example.toml config.toml
   ```
   Open `config.toml` and edit `library_root` under `[paths.windows]` or `[paths.linux]` to point to your ROM library folder or NAS mount.

3. **Install external tools** for the consoles you collect — see **[Tools & Keys](tools-and-keys.md)**.

4. **Verify your environment:**
   ```bash
   # Windows
   .\.venv\Scripts\python.exe main.py doctor

   # Linux / macOS
   ./.venv/bin/python main.py doctor
   ```

---

## Everyday CLI Commands

| Command | What It Does |
| :--- | :--- |
| `python main.py doctor` | Read-only diagnostics for Python packages, external tools, disk space, and write/atomic-rename access on `library_root` |
| `python main.py inspect <path...>` | Read-only archive & header peek showing detected `system_id` and planned conversion action |
| `python main.py process <path...> [--dry-run] [--keep-source]` | Extract, convert, and route specific file(s) or folder(s) |
| `python main.py scan [--dry-run] [--no-wait]` | One-shot sequential pass over everything currently sitting in `./drop` |
| `python main.py watch [--no-backlog]` | Continuous daemon monitoring `./drop` for newly dropped downloads/archives |

### Example `doctor` Output

![Project Mjolnir Doctor Diagnostics](assets/doctor.svg)

---

## Linux Notes

### Package Installation (Example: Fedora)
```bash
sudo dnf install 7zip mame-tools dolphin-emu-tool dotnet-runtime-10.0
```
- `7zip` provides `7z`, `mame-tools` provides `chdman`, `dolphin-emu-tool` provides `dolphin-tool`, and `dotnet-runtime-10.0` runs the Linux x64 `psxpackager` CLI in `tools/psxpackager/psxpackager` (Mjolnir sets `DOTNET_ROLL_FORWARD=LatestMajor` automatically).

### Full `.rar` Support on Linux (`7zz`)
Some Linux distro `7zip` packages (`/usr/bin/7z`) omit proprietary unRAR codecs due to licensing guidelines. Download the official standalone `7zz` executable into `tools/7z/7zz` (or let `python setup_mjolnir.py --tools 7z` fetch it for you):

```bash
mkdir -p tools/7z
curl -fL -o /tmp/7z.tar.xz https://github.com/ip7z/7zip/releases/download/26.06/7z2606-linux-x64.tar.xz
tar -xf /tmp/7z.tar.xz -C tools/7z 7zz
chmod +x tools/7z/7zz
```

Mjolnir probes `7z i` (cached) and automatically prioritizes a binary with RAR support over `/usr/bin/7z`.

### GNOME GVFS `smb://` Auto-Mount
On Linux desktops using GNOME Files, `[paths.linux] -> library_root` accepts either a standard mount path (`/mnt/nas/games/library/roms`) or a direct SMB URI (`smb://my-nas.local/games/library/roms`). When `auto_mount_smb = true`, Mjolnir resolves `/run/user/$UID/gvfs/` and attempts `gio mount` using saved GNOME Keyring credentials if the share is not yet mounted.
