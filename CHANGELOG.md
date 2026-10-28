# Changelog

All notable changes to Vanta are documented here. The format follows [Conventional Commits](https://www.conventionalcommits.org/).

## 0.1.1 (2026-10-28)

### New features

- New Vanta icon for the program, the in-app logo and the website favicon
- Azure is now the default accent color

## 0.1.0 (2026-10-28)

### New features

- **core:** Versions, downloads, Java, auth, loaders and launching
- **modrinth:** Typed Modrinth API client with dependency resolution
- **ui:** Design tokens, component library, logo, icons and Inter font
- **ui:** Shell, pages and dialogs with animated transitions
- **app:** Connect the UI to core: accounts, instances, launching, browse, settings, updater
- **web:** Landing page with OS-aware downloads and GitHub Pages deploy

### Performance

- Use the ring crypto provider and subset the bundled fonts
- Smaller release binary, software rendering option, measured numbers

### Documentation

- README, architecture, design system, releasing and community files

### Build and release

- Set up cargo workspace, CI and dependency policy
- Release workflow and packages for Windows and Linux
- Keep editor-local folders out of the shared ignore file
- **deps:** Update Slint to 1.18 and zip to 9, fix advisories
