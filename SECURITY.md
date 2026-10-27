# Security policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting:
**Security → Report a vulnerability** on <https://github.com/Inpriv/vanta>.

Include what you found, how to reproduce it and the impact you expect. You can
expect an acknowledgement within a few days. Fixes are released as soon as
possible and credited in the release notes unless you prefer otherwise.

## Scope and design notes

- Microsoft and Minecraft tokens are stored only in the operating system
  keychain and are never written to logs (launch commands are logged with the
  access token redacted).
- Update packages are verified against an Ed25519 (minisign) public key
  embedded in the binary before they are installed, and only after the user
  confirms.
- `.mrpack` imports reject absolute paths, `..` components and downloads from
  hosts outside the Modrinth specification's allow-list.
- All network traffic uses HTTPS with rustls; there is no OpenSSL dependency.
