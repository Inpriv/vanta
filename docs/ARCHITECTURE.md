# Architecture

Vanta is a Cargo workspace with three crates and two non-Rust directories.

```
crates/
  vanta-core      Everything that is not UI. No Slint, fully unit-tested.
  vanta-modrinth  Typed client for the Modrinth v2 API.
  vanta-app       The `vanta` binary: Slint UI + glue to core.
web/              Static website (plain HTML/CSS/JS), deployed with GitHub Pages.
packaging/        Icons, .desktop, AppStream metainfo, NSIS script, PKGBUILDs, signing key.
```

## Threads and responsiveness

- The **UI thread** runs the Slint event loop and only renders and reacts to input.
- A **Tokio runtime** (4 workers) does all disk and network I/O. UI callbacks
  spawn tasks and return immediately.
- Results come back with `slint::Weak::upgrade_in_event_loop`; see `Ctrl::with_ui`
  in `crates/vanta-app/src/app/mod.rs`.
- Slint models and images are not `Send`, so workers produce plain data or
  `SharedPixelBuffer`s and the UI thread wraps them.
- Download progress is a set of atomics (`download::Progress`) polled by a
  100 ms UI timer that only runs while something is downloading. Game logs go
  into a shared buffer that a 200 ms timer appends to a virtualised list.
  Idle, Vanta schedules no timers at all.

## vanta-core

| Module | Responsibility |
| --- | --- |
| `paths` | Data directory layout. Shared, content-addressed storage for libraries, assets, runtimes and mods. |
| `version`, `rules`, `maven` | Version manifest and version JSON model, `inheritsFrom` merging, rule evaluation, Maven coordinates. |
| `meta` | Fetching (and caching) the manifest and version JSONs. |
| `download` | Bounded-concurrency, resumable, retried, SHA-1-verified downloads. |
| `install` | Turns a resolved version into download tasks; extracts natives; legacy/virtual assets. |
| `java` | Java detection, Mojang runtime manifest downloads, Adoptium fallback. |
| `launch` | Command-line building (`args`) and process/log handling with crash detection (`process`). |
| `auth` | Device code → Xbox Live → XSTS → Minecraft Services → entitlements/profile. |
| `accounts` | Account list on disk, secrets through a `SecretStore` trait (the app uses the OS keychain). |
| `instance` | Instance model and store. |
| `loaders` | Fabric/Quilt (meta API profiles) and Forge/NeoForge (installer + processors, legacy installers). |
| `content` | Mods/resource packs/shaders of an instance; enable/disable; Modrinth metadata. |
| `mrpack` | `.mrpack` import/export with path and host validation. |
| `launcher` | Orchestration from an instance id to a running game. |

### Downloads

`Downloader::download_all` de-duplicates tasks by path and runs them with
`try_for_each_concurrent` (48 by default). Each file is streamed to
`<name>.part` while hashing, resumed with an HTTP `Range` request when a
partial file exists, verified, then atomically renamed. Server errors and
network failures retry with exponential backoff; 4xx responses do not.

Already-present files are trusted when their size matches (`Verify::Quick`);
files without a known size are re-hashed. `Verify::Full` re-hashes everything.

### Version resolution

A loader version (e.g. `fabric-loader-0.16.9-1.21.1`) inherits from a vanilla
version. `Meta::resolve` merges the chain: child scalar fields win, child
libraries come first and replace parent libraries with the same
`group:artifact[:classifier]`, and argument lists are concatenated. The merged
version records which vanilla jar it runs on; loader versions get a hard-linked
copy named after their own id because Forge looks the jar up by
`${version_name}.jar`.

### Forge and NeoForge

Modern installers (`install_profile.json` with `processors`) are handled by
downloading the official installer, unpacking its bundled `maven/` artifacts,
installing vanilla, resolving `data` values for the client side and running
each client processor with the vanilla version's Java. Outputs are checked
against the declared SHA-1s and processors are skipped if their outputs are
already correct. The version JSON is written last, so a half-finished install
is never considered complete. Legacy installers (≤ 1.12.2) embed the version
JSON (`versionInfo`) and the universal jar, which is extracted directly.

## vanta-app

- `ui/` contains the Slint sources: `theme.slint` (design tokens),
  `components.slint`, `shell.slint` (title bar, sidebar, resize handles),
  `pages/*`, `dialogs.slint` and `state.slint` (the `App` global with all
  data and callbacks).
- `src/app/*` binds those callbacks to `vanta-core` per feature area.
- `examples/screenshots.rs` renders the real UI headlessly with sample data;
  it produces the screenshots in the README and on the website.

### Window

The window is borderless (`no-frame`). Dragging and edge resizing use winit's
`drag_window` / `drag_resize_window` through Slint's `unstable-winit-030`
accessor. The Linux app id is set to `xyz.getvanta.Vanta` so desktops match it
to the `.desktop` file.

### Single instance

A lock file in the config directory is held with `File::try_lock`. The
primary instance listens on an ephemeral loopback port (written next to the
lock); a second launch connects, asks it to show its window and exits.

### Updates

`update.rs` reads the latest GitHub release, picks the asset for the current
install kind (installer, portable exe, AppImage, tarball; system packages are
pointed to their package manager), downloads it with its `.minisig`, verifies
it against the embedded public key and only then replaces the binary or runs
the installer. The user is asked first.

## Why Slint

Slint compiles the UI to native code, renders on the GPU (femtovg/OpenGL with a
software fallback), has declarative animations, and adds only a few megabytes
to the binary. It met every requirement, so no switch to another toolkit was
needed. Workarounds worth knowing:

- Properties of type `easing` cannot be exported to Rust, so `Motion` stays
  internal and its reduce-motion flag is bound to `App.reduce-motion`.
- Slint strings have no `contains`; the UI calls the pure callback
  `App.text-matches` implemented in Rust.
- `color.alpha` is on a 0–255 scale; translucent scrims use an explicit
  `scrim-alpha` fraction.
