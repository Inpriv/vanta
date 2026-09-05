<div align="center">

<img src="icons/icon.png" width="72" height="72" alt="Vanta">

# Vanta

### Minimalist. High-performance. Free.

**A non-premium Minecraft launcher engineered for instant play — pick a nickname, pick a version, and jump straight into the game.**

[Website](https://getvanta.xyz/) · [Releases](https://github.com/inpriv/vanta/releases) · [Issues](https://github.com/inpriv/vanta/issues) · [License](https://github.com/inpriv/vanta/blob/main/LICENSE)

</div>

---

## Overview

Vanta is a lightweight desktop launcher built with Python 3 and PyQt6. No login, no premium account, no hassle — enter any username, select a Minecraft version, and play. Installations are fully isolated per version, Fabric and performance mods are managed in-app, and the launcher updates itself from GitHub.

> **Why "non-premium"?** Vanta maps your nickname to a deterministic offline UUID (v3), so offline profiles keep a consistent identity across sessions and servers.

---

## Features

**Instant Play** — non-premium access: enter a username, pick a version, play.

**Auto-Updater** — checks GitHub on startup, downloads and verifies updates over HTTPS, swaps the executable atomically with rollback, and relaunches.

**Launch Watchdog** — game starts are confirmed from the game's own log output with a 90-second startup window, so silent crashes are caught and reported cleanly.

**Verified Installs** — fully installed versions are marked on disk; relaunches skip file checks entirely, and broken or partial installs are repaired automatically.

**Stable Offline UUIDs** — usernames map to a deterministic UUID (v3) for a consistent identity across sessions and servers.

**Mods Manager** — search, install, and manage Fabric mods per version via the Modrinth API, with SHA-1 verification on every file.

**Performance Mode** — one-click Fabric engine install with automatic injection of Sodium, Lithium, FerriteCore, and EntityCulling.

**Discord Rich Presence** — live playing status via pypresence, fully toggleable in the settings drawer.

**Isolated Instances** — settings, worlds, logs, and mods are kept strictly per-version inside `.Vanta/instances/<version>`.

**Custom RAM Allocation** — a 1–16 GB allocation slider, automatically capped to your system's actual capacity.

**Automated Java Management** — detects the exact Mojang-specified runtime (`java-runtime-delta`, `gamma`, `alpha`, or `jre-legacy`) and installs a portable, user-space JVM without admin rights.

**Smart Offline Mode** — if the Mojang API is unreachable, the version grid falls back to cached and locally installed versions automatically.

---

## Security

**HTTPS-only downloads** — mod and update URLs over plain HTTP are refused.

**SHA-1 verification** — every downloaded mod file is verified against the hash reported by the Modrinth API.

**Atomic writes** — downloads stream to a temporary `.part` file and move into place, so an interrupted download never leaves a corrupt jar.

**Validated updates** — launcher updates are checked (Windows PE header) before an atomic swap; a failed swap rolls back automatically.

**No telemetry** — the launcher talks only to Mojang's servers, the Modrinth API, and minotar.net for skins.

---

## Tech Stack

**Framework** — Python 3.10+, PyQt6 (borderless, fully animated UI)

**Minecraft core** — minecraft-launcher-lib (commands, runtimes, Fabric bootstrap)

**Networking** — requests (Mojang, Modrinth, update manifests)

**Rich Presence** — pypresence (optional, Discord IPC)

**System monitoring** — psutil (RAM detection)

**Packaging** — Nuitka (single native executable, no runtime self-extraction)

---

## Getting Started

### For Users

1. Head over to the [Releases](https://github.com/inpriv/vanta/releases) page.
2. Download the latest `Vanta.exe` (Windows).
3. Run it and start playing — the built-in auto-updater keeps the launcher current from here on.

### From Source

```bash
git clone https://github.com/inpriv/vanta.git
cd vanta
pip install -r requirements.txt
python main.py
```

### Building the Executable

The build uses **Nuitka** (not PyInstaller) to compile the launcher into a single native binary — no `%TEMP%\_MEIxxxx` runtime unpacking, which avoids antivirus false positives:

```bash
pip install nuitka zstandard
python build.py            # release build -> dist/Vanta.exe
python build.py --debug    # keep a console window for troubleshooting
```

On Windows you can also run `build.bat`. A C++ toolchain (MSVC or MinGW64) is required; if none is found, Nuitka downloads a private MinGW64 automatically. Ensure `icons/icon.ico` and `icons/icon.png` exist before building.

---

## Project Structure

<details>
<summary>Repository layout</summary>

```text
Vanta/
├── main.py               # Launcher entry point, UI, workers, game runtime
├── build.py              # Nuitka build script (onefile native binary)
├── build.bat             # Windows build wrapper
├── requirements.txt      # Runtime dependencies
├── version.json          # Update manifest (latest tag + download URL)
├── icons/
│   ├── icon.ico          # Window/taskbar icon
│   ├── icon.png          # Logo / README
│   └── icon-bg.png       # Social preview background
└── static/               # Website (getvanta.xyz)
    ├── index.html
    ├── robots.txt
    └── sitemap.xml
```

</details>

---

## Roadmap

- [x] Auto-updater with atomic swap and rollback
- [x] Launch watchdog with log-based startup confirmation
- [x] Mods manager with Modrinth search and SHA-1 verification
- [x] Performance mode (Sodium, Lithium, FerriteCore, EntityCulling)
- [x] Nuitka native build pipeline
- [ ] Linux and macOS builds
- [ ] Microsoft account login (optional premium support)
- [ ] Instance import/export

---

## Contributing

1. Open an issue before large changes.
2. Keep the codebase dependency-light.
3. Preserve the no-telemetry guarantee — no analytics, ever.

---

## License

MIT © 2026 [Inpriv Labs](https://inpriv.xyz)

<div align="center">

Built for players — by **Inpriv Labs**, independent studio

</div>