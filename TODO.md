# Vanta — roadmap to v0.1.0

Status legend: `[x]` done · `[~]` in progress · `[ ]` not started

## M1 — Workspace + CI
- [x] Cargo workspace (`vanta-core`, `vanta-modrinth`, `vanta-app`), edition 2024, release profile
- [x] CI: fmt, clippy `-D warnings`, test, Windows + Linux build, `cargo deny`
- [x] `deny.toml`, `rustfmt.toml`, `.gitignore`, `dependabot.yml`

## M2 — Core: vanilla launch
- [x] Version manifest (piston-meta), version JSON model, `inheritsFrom` merge
- [x] Rule evaluation (OS name/arch/version, features)
- [x] Download engine: bounded concurrency, retries + backoff, resume, SHA-1 verify, progress events
- [x] Content-addressed shared store (libraries, assets, client jars)
- [x] Natives extraction
- [x] Java: detection, Mojang java-runtime download, per-instance override
- [x] Launch argument builder (JVM + game args, placeholders, legacy `minecraftArguments`)
- [x] Process spawn, log streaming, crash detection

## M3 — Auth
- [x] MSA device code flow → Xbox Live → XSTS → Minecraft Services → profile/ownership
- [x] Token refresh, multi-account store, tokens in OS keychain only
- [x] Offline launch only for accounts that verified ownership

## M4 — UI shell + design system
- [x] Tokens (color, type, spacing, radius, shadow, motion), bundled font
- [x] Borderless window + custom title bar, sidebar, page transitions, reduce motion
- [x] Logo + app icon

## M5 — Instances
- [x] Create / duplicate / rename / delete / icon / open folder
- [x] Per-instance settings: version, loader, RAM, JVM args, resolution, Java

## M6 — Mod loaders
- [x] Fabric, Quilt (meta API profiles)
- [x] Forge, NeoForge (installer + processors)

## M7 — Modrinth + mrpack
- [x] Modrinth client: search, facets, versions, dependencies
- [x] Browse UI: install with deps, update, enable/disable, remove
- [x] `.mrpack` import / export

## M8 — Settings + updater
- [x] Settings page (theme, accent, motion, RAM, Java, close behavior, data dir)
- [x] Signed auto-update via GitHub Releases (minisign/ed25519), user confirmation
- [x] Single-instance guard

## M9 — Packaging + release workflow
- [x] Windows: NSIS installer (per-user), portable exe, optional code signing
- [x] Linux: AppImage, .deb, Arch .pkg.tar.zst, AUR `vanta-bin` PKGBUILD, tar.gz
- [x] SHA256SUMS, updater signatures, git-cliff release notes

## M10 — Website
- [x] Static site in `web/`, shared tokens, OS-detecting download button
- [x] GitHub Pages workflow, `CNAME`

## M11 — Docs + polish
- [x] README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CHANGELOG, templates
- [x] docs/ARCHITECTURE, DESIGN, PERFORMANCE (real measurements), RELEASING
- [ ] Performance pass against targets; tag v0.1.0

## Blocked on the maintainer
- [ ] Repository name `Inpriv/vanta` is taken by an older project; waiting for it to be freed before pushing
