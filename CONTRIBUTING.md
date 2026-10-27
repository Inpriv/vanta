# Contributing to Vanta

Thanks for helping! Bug reports, fixes, features and documentation are all welcome.

## Getting started

1. Install Rust stable (1.92+) and the system libraries listed in the README.
2. `cargo run -p vanta-app` starts the launcher in debug mode.
3. Set `VANTA_LOG=debug` for verbose logs (written to the `logs/` folder in the data directory).
4. To try sign-in you need your own Azure client ID (`VANTA_MSA_CLIENT_ID`, see README).

Tip: to keep development data away from your real install, point the XDG
directories somewhere else, e.g.
`XDG_CONFIG_HOME=/tmp/vc XDG_DATA_HOME=/tmp/vd cargo run -p vanta-app`.

## Before you open a pull request

```sh
cargo fmt --all
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

- Logic belongs in `vanta-core` (or `vanta-modrinth`) with unit tests; `vanta-app` should stay thin glue.
- Never block the UI thread: do I/O on the Tokio runtime and send results back with `Ctrl::with_ui`.
- Follow the design tokens in `ui/theme.slint` and `docs/DESIGN.md`; don't hardcode colors or durations in components.
- Every new interactive element needs hover, pressed, focus and disabled states and an accessible label.
- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `ci:`, `chore:`). Release notes are generated from them.
- Keep pull requests focused. Explain *why* in the description.

## Screenshots

`cargo run -p vanta-app --example screenshots -- <dir>` renders every page with
sample data. Update `web/assets/screenshots/` if you change the UI noticeably.

## License

By contributing you agree that your work is licensed under GPL-3.0-or-later.
