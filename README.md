<div align="center">

<img src="icons/icon.png" width="88" alt="Vanta Launcher logo">

# Vanta Launcher

### The free Minecraft launcher that gets out of your way.

**Pick a nickname. Pick a version. Play.**

[![Release](https://img.shields.io/github/v/release/inpriv/vanta?style=flat-square)](https://github.com/inpriv/vanta/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows-blue?style=flat-square)](https://github.com/inpriv/vanta/releases/latest)
[![License](https://img.shields.io/github/license/inpriv/vanta?style=flat-square)](LICENSE)
[![Packaging](https://img.shields.io/badge/packaging-Nuitka%20native-9F86FF?style=flat-square)](#build-from-source)
[![Website](https://img.shields.io/badge/website-getvanta.xyz-E75480?style=flat-square)](https://getvanta.xyz/)

**[Download the latest release](https://github.com/inpriv/vanta/releases/latest)** · [Website](https://getvanta.xyz/) · [Report an issue](https://github.com/inpriv/vanta/issues)

</div>

---

## Why Vanta

Most launchers want an account first and ask questions later. Vanta flips that: enter any nickname, choose a Minecraft version, and hit play — no login, no premium, no hassle. Fabric, performance mods, and the right Java runtime are set up for you automatically, and every game version lives in its own clean instance so mods and saves never collide.

> **Why "non-premium"?** Nicknames map to a deterministic offline UUID (v3), so your offline profile keeps the same identity across sessions and servers.

## Download & play

1. Grab the latest **`Vanta.exe`** (94.7 MB) from the [Releases](https://github.com/inpriv/vanta/releases/latest) page — or the standalone ZIP **`Vanta-1.9-standalone.zip`** (36 MB; see [antivirus notes](#antivirus-false-positives))
2. Run it — no installer, no admin rights
3. Type a nickname, pick a version, press **Play**

That's it. The launcher keeps itself up to date from here on.

## Features

**Instant play** — non-premium access: enter a username, pick a version, play.

**Auto-updater** — checks GitHub on startup, verifies updates over HTTPS, swaps the executable atomically with rollback, and relaunches.

**Launch watchdog** — confirms startup from the game's own log output within a 90-second window, so silent crashes are caught and reported with exit code and last log lines.

**Verified installs** — fully installed versions are marked on disk; relaunches skip file checks, and broken or partial installs are repaired automatically.

**Stable offline UUIDs** — deterministic v3 UUIDs keep your offline identity consistent across sessions and servers.

**Mods manager** — search, install, and manage Fabric mods per version via the Modrinth API, with SHA-1 verification on every file.

**Performance mode** — one-click Fabric setup with automatic Sodium, Lithium, FerriteCore, and EntityCulling injection.

**Discord Rich Presence** — live playing status via pypresence, fully toggleable in the settings drawer.

**Isolated instances** — settings, worlds, logs, and mods stay per-version inside `.Vanta/instances/<version>`.

**Custom RAM allocation** — a 1–16 GB slider, capped to your system's actual capacity.

**Automated Java management** — detects the exact Mojang-specified runtime and installs a portable, user-space JVM without admin rights.

**Smart offline mode** — if Mojang's API is unreachable, the version grid falls back to cached and locally installed versions.

## Antivirus false positives

> [!IMPORTANT]
> **Windows Defender may flag the single-file `Vanta.exe` as `Trojan:Win32/Wacatac.B!ml`.** This is a false positive: ML heuristics distrust unsigned, self-extracting executables. It is not a trojan.

Vanta is MIT-licensed open source, contains **no telemetry**, and talks only to Mojang's servers, the Modrinth API, and minotar.net for skins. Three ways forward:

1. **Use the standalone ZIP** from the release page — no self-extraction, the most antivirus-friendly packaging
2. **Verify the SHA-256** of your download (below) and restore the file from quarantine
3. **Report the false positive** at [microsoft.com/wdsi/filesubmission](https://www.microsoft.com/en-us/wdsi/filesubmission) (choose *Software developer*) — this whitelists the build for everyone within days

## Verify your download

Every release publishes its SHA-256 on the release page. Current (v1.9):

```
5a516377fc15b5df039780dde39f5c97ee7912f63e49e69dae4499caefb1045a
```

```powershell
Get-FileHash .\Vanta.exe -Algorithm SHA256
```

The standalone ZIP (`Vanta-1.9-standalone.zip`) hashes to:

```
d2ad50a7e85d14b0e85d3ea0d420def94e9834addffaf1e0775b8d30d9174730
```

## Security

**HTTPS-only downloads** — plain-HTTP mod and update URLs are refused.

**SHA-1 verification** — every downloaded mod is checked against the hash reported by the Modrinth API.

**Atomic writes** — downloads stream to a temporary `.part` file and move into place, so an interrupted download never leaves a corrupt jar.

**Validated updates** — launcher updates are checked (Windows PE header) before an atomic swap; failed swaps roll back automatically.

**No telemetry** — the launcher talks only to Mojang's servers, the Modrinth API, and minotar.net for skins.

## Build from source

**Run it:**

```bash
git clone https://github.com/inpriv/vanta.git
cd vanta
pip install -r requirements.txt
python main.py
```

**Build the executable** — Nuitka, not PyInstaller: real native code, no `%TEMP%\_MEIxxxx` self-extraction:

```bash
pip install nuitka zstandard
python build.py                # single file  -> dist/Vanta.exe
python build.py --standalone   # app folder   -> dist/main.dist/
```

`build.bat` does the same on Windows. A C++ toolchain (MSVC or MinGW64) is required; if none is found, Nuitka downloads a private MinGW64 automatically.

## Project structure

<details>
<summary>Repository layout</summary>

```text
Vanta/
├── main.py               # Launcher entry point, UI, workers, game runtime
├── build.py              # Nuitka build script (onefile / --standalone)
├── build.bat             # Windows build wrapper
├── requirements.txt      # Runtime dependencies
├── version.json          # Update manifest (latest tag + download URL)
├── wrangler.toml         # Cloudflare Pages config for the website
├── icons/                # Window icon, logo, social preview
└── static/               # Website (getvanta.xyz)
    ├── index.html
    ├── icons/            # Self-hosted site assets
    ├── robots.txt
    └── sitemap.xml
```

</details>

## Roadmap

- [x] Auto-updater with atomic swap and rollback
- [x] Log-based launch watchdog
- [x] Mods manager with Modrinth search and SHA-1 verification
- [x] Performance mode (Sodium, Lithium, FerriteCore, EntityCulling)
- [x] Nuitka native build pipeline
- [x] Standalone (folder) distribution
- [ ] Linux and macOS builds
- [ ] Microsoft account login (optional premium support)
- [ ] Code signing for flag-free downloads
- [ ] Instance import/export

## Contributing

1. Open an issue before large changes.
2. Keep the codebase dependency-light.
3. Preserve the no-telemetry guarantee — no analytics, ever.

## License

MIT © 2026 [Inpriv Labs](https://inpriv.xyz)

<div align="center">

**[Download](https://github.com/inpriv/vanta/releases/latest)** · [Website](https://getvanta.xyz/) · [Report an issue](https://github.com/inpriv/vanta/issues)

Built for players — by **Inpriv Labs**

</div>
