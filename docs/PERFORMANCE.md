# Performance

All numbers are real measurements of the v0.1.6 release binary
(`Vanta-linux-x86_64.tar.gz` from the GitHub release) taken with [`scripts/bench.sh`](../scripts/bench.sh).

**Machine:** Intel Core i5-12500H laptop (12 cores / 16 threads), 16 GB RAM,
NVMe SSD, NVIDIA GPU with the proprietary 615.78 driver, Arch Linux
(kernel 7.2), GNOME on Wayland (Vanta running through XWayland/EGL).
Measured on 2026-10-28.

## Results

| Metric | Target | Measured | |
| --- | --- | --- | --- |
| Cold start → first painted frame | < 200 ms (aim < 100 ms) | **134 ms** median (128–150 ms, 15 runs) | ✅ |
| Process start → exit right after first frame (wall clock) | — | 168 ms median | |
| Idle CPU | none | **0** clock ticks over 5 s | ✅ |
| Idle memory, Vanta's own (anonymous) | < 60 MB | **28 MiB** (GPU renderer) | ✅ |
| Idle memory, whole process RSS | < 60 MB | **148 MiB** GPU renderer | ⚠️ see below |
| Release binary (Linux x86_64) | < 20 MB | **28.4 MiB** | ❌ see below |
| Install vanilla 1.20.1 from scratch | — | 3,629 files / 733 MB in **≈10 s** (network-bound) | |
| Re-verify an installed version before launch | — | **0.01 s** | |
| Animations | 60 fps | GPU-rendered; durations 120–260 ms, no layout work during animation | ✅ |

### Startup

The UI is compiled to native code by Slint, fonts and icons are embedded, and
nothing that touches the network or scans the disk runs before the first frame:
instances, accounts and update checks load on the Tokio runtime afterwards and
stream into the UI. The 134 ms are measured inside the process from `main()` to
the end of the first `AfterRendering` callback of the OpenGL renderer, so they
include creating the GPU context. (Slint only reports rendering callbacks for
OpenGL, so this benchmark measures the default GPU renderer.)

### Memory

Per mapping (GPU renderer, `/proc/<pid>/smaps`):

| Mapping | RSS |
| --- | --- |
| NVIDIA driver (`libnvidia-gpucomp`, `libnvidia-eglcore`, `/dev/nvidiactl`, EGL) | ≈ 50 MiB |
| Mesa (`libLLVM`, `libgallium`), loaded during EGL device probing | ≈ 17 MiB |
| Vanta binary (clean, shared file pages) | ≈ 20 MiB |
| Heap and other anonymous memory (Vanta + driver) | ≈ 27 MiB |

So the RSS target is missed **because of the GPU driver**, not Vanta: the app's
own memory is about 28 MiB, and with **Settings → Appearance → Software
rendering** the whole process used 43 MiB RSS in the v0.1.0 measurement. Memory-saving measures in the
app itself:

- Modrinth icons are decoded off the UI thread and downscaled to 96 px; skin
  heads are 8×8 pixels; the in-memory image cache is bounded.
- Game logs are kept in one bounded buffer (50,000 lines) and shown through a
  virtualised list capped at 20,000 rows.
- No timers run while idle (progress and log timers start and stop on demand).

### Binary size

The Linux binary is 28.4 MiB (stripped, fat LTO, `panic = "abort"`); it was
25.9 MiB in v0.1.0.
`cargo bloat` shows the size is spread across many medium-sized parts rather
than one outlier: Rust std (1.5 MiB), Slint core/backend/renderers (≈ 2 MiB),
Slint-generated UI code, the text stack (skrifa, harfrust, swash, parley),
the SVG stack that Slint's GPU renderer requires (usvg, resvg, tiny-skia,
≈ 0.9 MiB), D-Bus for accessibility, keychain and portals (zbus, zvariant,
dbus, accesskit), winit with X11 and Wayland, Tokio, rustls/ring, HTTP/2 and
image decoders.

What was done:

| Change | Effect |
| --- | --- |
| rustls with `ring` instead of `aws-lc-rs` | removes the aws-lc C library |
| Inter subset to Latin ranges | 1.65 MB → 0.59 MB of embedded fonts |
| `opt-level = "s"` for dependencies that are not on a hot path; Vanta's core, Tokio, TLS, hashing, compression and JSON stay at `opt-level = 3` | −1.3 MiB |
| No `regex` (simple log level filter), no zopfli, no GIF decoding, `arboard` without image support | −0.3 MiB |

For reference, building *everything* with `opt-level = "z"` still gives
21.4 MiB, so reaching 20 MiB on Linux would mean dropping something users
rely on (screen-reader support, the native file dialogs, Wayland or X11, the
software-rendering fallback, or WebP icons). Those trade-offs were not made.
Windows builds do not include the D-Bus, X11 and Wayland stacks. The
installer, AppImage and packages are compressed; their exact sizes are listed
on each release.

## Reproducing

```sh
cargo build --release -p vanta-app
scripts/bench.sh target/release/vanta 15
```

`VANTA_BENCH_STARTUP=1 vanta` prints `first_frame_ms=…` and exits after the
first frame. Size analysis: `cargo bloat --profile bloat -p vanta-app --bin vanta --crates`.

## Website

Lighthouse 13 (headless Chrome 156) on `web/`, 2026-10-28:

| Form factor | Performance | Accessibility | Best practices | SEO |
| --- | --- | --- | --- | --- |
| Mobile | 100 | 100 | 96 | 100 |
| Desktop | 100 | 100 | 96 | 100 |

The page is plain HTML/CSS with one small deferred script, self-hosted Latin
subsets of Inter (≈ 25 KB per weight) and WebP screenshots. The best-practices
point was a console error from the GitHub releases API returning 404 before the
first release existed.
