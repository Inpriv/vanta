# Changelog

All notable changes to Vanta are documented here. The format follows [Conventional Commits](https://www.conventionalcommits.org/).

## 0.1.9 (2026-10-28)

### New features

- The pixel Vanta logo is now the app icon everywhere: the window and taskbar, the Windows .exe and installer, Linux menus and the AppImage

## 0.1.8 (2026-10-28)

### Fixes

- Block icons have rounded corners with software rendering too, instead of a square block over a rounded tile
- The glow on the home screen stays centred behind the instance icon in smaller windows

## 0.1.7 (2026-10-28)

### New features

- Mod, pack and shader icons are rounded squares in Browse, the project page and an instance's content list (also with software rendering)

## 0.1.6 (2026-10-28)

### New features

- A new pixel look built around the block icons: Pixelify Sans for titles, labels and buttons, pixel icons, block-style buttons with hard shadows, square controls and a faint pixel grain
- Pixel loading screen: the V builds up in steps, the name types in and a block bar fills
- Pixel version of the Vanta logo and a stepped pixel spinner

## 0.1.5 (2026-10-28)

### New features

- Opening animation: the Vanta mark rises in with an accent glow and the name appears letter by letter (click to skip; off with "Reduce motion")
- New Linux download `Vanta-linux-x86_64.tar.gz`: extract it and double-click `vanta`, no extra steps

### Fixes

- The new-instance icon tooltip no longer covers the mod loader label

## 0.1.4 (2026-10-28)

### New features

- A single Play control: it fills with live download progress, then shows "Playing" with the play time and a Stop button
- 22 original pixel-art block icons for instances, plus the Vanta mark in your accent color (the new default)
- The AppImage shows the Vanta icon in file managers

### Fixes

- "Starting game" no longer stays on screen while the game is running
- Shorter progress labels that fit the Play control
- Release signing works with the passwordless signing key

## 0.1.3 (2026-10-28)

### New features

- Play with an offline account: just pick a username. The same name always keeps the same player, so inventories and progress persist, also on friends' LAN worlds
- Microsoft sign-in is shown as coming soon in official builds
- New instances get an icon in your accent color with the first letter of their name
- Smaller default window (800×520)

### Fixes

- Text in dialogs, the console and notifications is sharp
- Notifications hide after four seconds and their text is centered
- The icon picker no longer overflows the new-instance dialog
- Windows installer builds again in the release workflow

## 0.1.2 (2026-10-28)

### New features

- Rounded window corners and rounded window controls
- Clicking a Browse result opens its details inside Vanta; Install always works and asks which instance to use, or offers to create one
- Compact new-instance dialog with a working mod loader picker; Enter creates the instance
- Play starts Microsoft sign-in when you are not signed in yet
- Slim, draggable scrollbars; memory shown in GB; the first Java download is labelled
- Smaller default window and a crisp vector logo

### Fixes

- Dragging a slider no longer scrolls the page
- The loading spinner is smooth and sits inside its button
- Dialogs scroll when the window is too small, so their buttons stay reachable

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
