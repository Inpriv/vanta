# Vanta — roadmap to v0.1.0

Status legend: `[x]` done · `[~]` in progress · `[ ]` not started

## M1 — Workspace + CI
- [x] Cargo workspace (`vanta-core`, `vanta-modrinth`, `vanta-app`), edition 2024, release profile
- [ ] CI: fmt, clippy `-D warnings`, test, Windows + Linux build, `cargo deny`
- [ ] `deny.toml`, `rustfmt.toml`, `.gitignore`, `dependabot.yml`

## M2 — Core: vanilla launch
- [ ] Version manifest (piston-meta), version JSON model, `inheritsFrom` merge
- [ ] Rule evaluation (OS name/arch/version, features)
- [ ] Download engine: bounded concurrency, retries + backoff, resume, SHA-1 verify, progress events
- [ ] Content-addressed shared store (libraries, assets, client jars)
- [ ] Natives extraction
- [ ] Java: detection, Mojang java-runtime download, per-instance override
- [ ] Launch argument builder (JVM + game args, placeholders, legacy `minecraftArguments`)
- [ ] Process spawn, log streaming, crash detection

## M3 — Auth
- [ ] MSA device code flow → Xbox Live → XSTS → Minecraft Services → profile/ownership
- [ ] Token refresh, multi-account store, tokens in OS keychain only
- [ ] Offline launch only for accounts that verified ownership

## M4 — UI shell + design system
- [ ] Tokens (color, type, spacing, radius, shadow, motion), bundled font
- [ ] Borderless window + custom title bar, sidebar, page transitions, reduce motion
- [ ] Logo + app icon

## M5 — Instances
- [ ] Create / duplicate / rename / delete / icon / open folder
- [ ] Per-instance settings: version, loader, RAM, JVM args, resolution, Java

## M6 — Mod loaders
- [ ] Fabric, Quilt (meta API profiles)
- [ ] Forge, NeoForge (installer + processors)

## M7 — Modrinth + mrpack
- [ ] Modrinth client: search, facets, versions, dependencies
- [ ] Browse UI: install with deps, update, enable/disable, remove
- [ ] `.mrpack` import / export

## M8 — Settings + updater
- [ ] Settings page (theme, accent, motion, RAM, Java, close behavior, data dir)
- [ ] Signed auto-update via GitHub Releases (minisign/ed25519), user confirmation
- [ ] Single-instance guard

## M9 — Packaging + release workflow
- [ ] Windows: NSIS installer (per-user), portable exe, optional code signing
- [ ] Linux: AppImage, .deb, Arch .pkg.tar.zst, AUR `vanta-bin` PKGBUILD, tar.gz
- [ ] SHA256SUMS, updater signatures, git-cliff release notes

## M10 — Website
- [ ] Static site in `web/`, shared tokens, OS-detecting download button
- [ ] GitHub Pages workflow, `CNAME`

## M11 — Docs + polish
- [ ] README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CHANGELOG, templates
- [ ] docs/ARCHITECTURE, DESIGN, PERFORMANCE (real measurements), RELEASING
- [ ] Performance pass against targets; tag v0.1.0

## Blocked on the maintainer
- [ ] Repository name `Inpriv/vanta` is taken by an older project; waiting for it to be freed before pushing
