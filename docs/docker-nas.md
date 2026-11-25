# Docker, NAS, Syncthing & ROMarr

Project Mjolnir is designed to run 24/7 headless in Docker directly on a NAS or home server via `Dockerfile` and `config.docker.example.toml` (leaving `config.toml` untouched for desktop machines).

---

## Multi-Stage Docker Image Contents

The included `Dockerfile` uses a two-stage Fedora build so the runtime image stays lean without compilers or build headers:

- **Runtime:** Fedora + Python 3.11 + `pycryptodome` + `pyctr` + `nsz`
- **Archives & Discs:** Standalone `7zz` (with RAR/RAR5 codecs), `chdman` (`mame-tools`), `dolphin-tool` (`dolphin-emu-tool`)
- **Compiled Console Tools (`/usr/local/bin/`):** Linux x64 `zarchive`, `cdecrypt`, and `pkg2zip`
- **PS1 Multi-Disc:** `.NET 10` runtime (`dotnet-runtime-10.0`) for optional host-mounted `psxpackager`
- **Nintendo Switch:** `NSC_Builder` (`squirrel.py` in `/opt/nscb/`) + automatic `prod.keys` → `keys.txt` sync

---

## Recommended Container Volume Mounts

Copy `config.docker.example.toml` to `config.docker.toml` (optional — the image falls back to `config.docker.example.toml` automatically if absent) and bind-mount your host paths in `docker-compose.yml`:

| Container Path | Purpose | Mode |
| :--- | :--- | :--- |
| `/app/drop` | Watched intake folder (`watch_dir`) | Read/Write |
| `/library` | Destination ROM library (`library_root`) | Read/Write |
| `/app/processed` | Completed originals when `post_process_action = "move"` | Read/Write |
| `/app/quarantine` | Unidentified or encrypted items needing attention | Read/Write |
| `/app/tools` | Optional host folder for user-supplied keys (`tools/3ds/`, `tools/nscb/ztools/prod.keys`), `tools/psxpackager/`, or `tools/psvita/*.tsv` | Read-Only (`:ro`) |

### Example Stack Commands
```bash
cp config.docker.example.toml config.docker.toml
cd /path/to/stacks/mjolnir
docker compose up -d --build
docker exec mjolnir /app/.venv/bin/python /app/main.py --config /app/config.docker.toml doctor
docker logs -f mjolnir
```

---

## Syncthing & Samba-Safe Atomic Transfers

Project Mjolnir is built to coexist safely with Syncthing and Samba (SMB) shares syncing directly to handhelds (such as an AYN Thor running Cocoon FE):

1. All extractions and conversions happen locally inside lock-protected `.staging/job_<id>/` directories.
2. Finished files stream to `~syncthing~<filename>.tmp` at the destination (ignored by Syncthing so partial transfers are never synced, and avoiding leading-dot filenames that trigger Windows/Samba `Hidden` attributes).
3. Final byte sizes are verified before revealing the file atomically via `os.replace()`.
4. Non-PS1 multi-disc sets store raw discs in `_discs/` and expose a single POSIX `/` `.m3u` playlist at the system root; 3DS `.cia` packages route to `_cia_install/`; PS Vita add-ons route to `_updates_dlc/`.

---

## ROMarr Two-Library Integration

Project Mjolnir watches both top-level drops and 1-level platform bucket subdirectories (`drop/<platform>/<file>`) created by ROMarr's `Folder` library:

1. Set ROMarr's default intake library to your Mjolnir `drop/` path (e.g., `/path/to/mjolnir/drop`).
2. Add your destination ROM library (`/path/to/library/roms`) as a second catalog library so ROMarr continues tracking converted `.chd`/`.rvz`/`.pbp`/`.xci`/`.cci`/`.wua`/`.zip` files after Mjolnir processes `drop/`.
