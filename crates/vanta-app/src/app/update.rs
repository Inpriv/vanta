//! Self-update from GitHub Releases. Every package is verified against the
//! embedded minisign (Ed25519) public key before it is used, and nothing is
//! installed without the user's confirmation.

use std::path::{Path, PathBuf};

use serde::Deserialize;
use slint::ComponentHandle;

use super::Ctrl;
use crate::{App, AppWindow};

/// Public half of the release signing key (`packaging/minisign.pub`).
pub const PUBLIC_KEY: &str = "RWTbiE3xrAvE+eq5nSz7s2bguvTfttz2FdYywRrRa6DCeSv8VCMZ0kFP";
const LATEST_URL: &str = "https://api.github.com/repos/Inpriv/vanta/releases/latest";
const RELEASES_PAGE: &str = "https://github.com/Inpriv/vanta/releases/latest";

#[derive(Clone, Debug, PartialEq)]
pub enum InstallKind {
    WindowsInstaller,
    WindowsPortable(PathBuf),
    AppImage(PathBuf),
    Binary(PathBuf),
    /// Installed through a system package (deb, pacman, AUR).
    PackageManager,
}

#[derive(Clone, Debug)]
pub struct Release {
    pub version: semver::Version,
    pub notes: String,
    pub asset: Option<(String, String)>,
    pub kind: InstallKind,
}

#[derive(Deserialize)]
struct GhRelease {
    tag_name: String,
    #[serde(default)]
    body: String,
    #[serde(default)]
    assets: Vec<GhAsset>,
}

#[derive(Deserialize)]
struct GhAsset {
    name: String,
    browser_download_url: String,
}

pub fn install_kind() -> InstallKind {
    let exe = std::env::current_exe().unwrap_or_default();
    if cfg!(windows) {
        if exe.parent().is_some_and(|d| d.join("uninstall.exe").exists()) {
            InstallKind::WindowsInstaller
        } else {
            InstallKind::WindowsPortable(exe)
        }
    } else if let Some(appimage) = std::env::var_os("APPIMAGE") {
        InstallKind::AppImage(PathBuf::from(appimage))
    } else if exe.starts_with("/usr") || exe.starts_with("/opt") {
        InstallKind::PackageManager
    } else {
        InstallKind::Binary(exe)
    }
}

/// Asset name for this install kind and version.
pub fn asset_name(kind: &InstallKind, v: &semver::Version) -> Option<String> {
    Some(match kind {
        InstallKind::WindowsInstaller => format!("Vanta-Setup-{v}.exe"),
        InstallKind::WindowsPortable(_) => format!("Vanta-{v}-portable.exe"),
        InstallKind::AppImage(_) => format!("Vanta-{v}-x86_64.AppImage"),
        InstallKind::Binary(_) => format!("vanta-{v}-x86_64-linux.tar.gz"),
        InstallKind::PackageManager => return None,
    })
}

pub fn verify(data: &[u8], signature: &str) -> Result<(), String> {
    let key = minisign_verify::PublicKey::from_base64(PUBLIC_KEY).map_err(|e| e.to_string())?;
    let sig =
        minisign_verify::Signature::decode(signature).map_err(|e| format!("invalid signature file: {e}"))?;
    key.verify(data, &sig, false)
        .map_err(|_| "signature does not match Vanta's release key".to_owned())
}

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();
    app.on_check_for_updates({
        let ctrl = ctrl.clone();
        move || check(&ctrl, true)
    });
    app.on_install_update({
        let ctrl = ctrl.clone();
        move || install(&ctrl)
    });
    cleanup_old_binary();
}

fn status(ctrl: &Ctrl, busy: bool, text: impl Into<String>) {
    let text = text.into();
    ctrl.with_ui(move |ui| {
        let app = ui.global::<App>();
        app.set_update_busy(busy);
        app.set_update_status(text.into());
    });
}

pub fn check(ctrl: &Ctrl, manual: bool) {
    status(ctrl, true, "Checking for updates…");
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let result = async {
            let res = ctrl
                .shared
                .launcher
                .client
                .get(LATEST_URL)
                .header("Accept", "application/vnd.github+json")
                .send()
                .await?;
            if res.status().as_u16() == 404 {
                return anyhow::Ok(None);
            }
            let gh: GhRelease = res.error_for_status()?.json().await?;
            let version = semver::Version::parse(gh.tag_name.trim_start_matches('v'))?;
            let current = semver::Version::parse(env!("CARGO_PKG_VERSION"))?;
            if version <= current {
                return Ok(None);
            }
            let kind = install_kind();
            let asset = asset_name(&kind, &version).and_then(|name| {
                let url = |n: &str| {
                    gh.assets
                        .iter()
                        .find(|a| a.name == n)
                        .map(|a| a.browser_download_url.clone())
                };
                Some((url(&name)?, url(&format!("{name}.minisig"))?))
            });
            Ok(Some(Release {
                version,
                notes: gh.body,
                asset,
                kind,
            }))
        }
        .await;
        match result {
            Ok(Some(release)) => {
                let (v, notes) = (release.version.to_string(), release.notes.clone());
                let pm = release.kind == InstallKind::PackageManager;
                *ctrl.shared.update.lock().unwrap() = Some(release);
                status(
                    &ctrl,
                    false,
                    if pm {
                        format!("Version {v} is available from your package manager.")
                    } else {
                        format!("Version {v} is available.")
                    },
                );
                ctrl.with_ui(move |ui| {
                    let app = ui.global::<App>();
                    app.set_update_available(true);
                    app.set_update_version(v.into());
                    app.set_update_notes(if notes.trim().is_empty() {
                        "No release notes.".into()
                    } else {
                        notes.into()
                    });
                    app.set_update_open(true);
                });
            }
            Ok(None) => status(
                &ctrl,
                false,
                if manual {
                    "You're on the latest version."
                } else {
                    ""
                },
            ),
            Err(e) => {
                tracing::info!(error = %e, "update check failed");
                status(
                    &ctrl,
                    false,
                    if manual {
                        format!("Could not check for updates: {e}")
                    } else {
                        String::new()
                    },
                );
            }
        }
    });
}

fn install(ctrl: &Ctrl) {
    let Some(release) = ctrl.shared.update.lock().unwrap().clone() else {
        return;
    };
    let Some((url, sig_url)) = release.asset.clone() else {
        let _ = open::that_detached(RELEASES_PAGE);
        ctrl.with_ui(|ui| ui.global::<App>().set_update_open(false));
        return;
    };
    status(ctrl, true, "Downloading the update…");
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let result = async {
            let client = &ctrl.shared.launcher.client;
            let sig = client
                .get(&sig_url)
                .send()
                .await?
                .error_for_status()?
                .text()
                .await?;
            let data = client
                .get(&url)
                .send()
                .await?
                .error_for_status()?
                .bytes()
                .await?
                .to_vec();
            verify(&data, &sig).map_err(anyhow::Error::msg)?;
            let dir = ctrl.shared.launcher.paths.temp_dir();
            tokio::fs::create_dir_all(&dir).await?;
            let kind = release.kind.clone();
            tokio::task::spawn_blocking(move || apply(&kind, &data, &dir)).await??;
            anyhow::Ok(())
        }
        .await;
        match result {
            Ok(()) => {
                status(&ctrl, false, "Restarting…");
                let _ = slint::invoke_from_event_loop(|| {
                    let _ = slint::quit_event_loop();
                });
            }
            Err(e) => status(&ctrl, false, format!("Update failed: {e}")),
        }
    });
}

fn apply(kind: &InstallKind, data: &[u8], tmp: &Path) -> anyhow::Result<()> {
    match kind {
        InstallKind::WindowsInstaller => {
            let setup = tmp.join("Vanta-Setup.exe");
            std::fs::write(&setup, data)?;
            // Silent, per-user install; the installer starts Vanta afterwards.
            std::process::Command::new(&setup)
                .args(["/S", "/RESTART"])
                .spawn()?;
        }
        InstallKind::WindowsPortable(exe) | InstallKind::AppImage(exe) => {
            replace_and_restart(exe, data)?;
        }
        InstallKind::Binary(exe) => {
            let gz = flate2_decode(data)?;
            replace_and_restart(exe, &gz)?;
        }
        InstallKind::PackageManager => anyhow::bail!("update through your package manager"),
    }
    Ok(())
}

/// Extract the `vanta` binary from a release tarball.
fn flate2_decode(data: &[u8]) -> anyhow::Result<Vec<u8>> {
    use std::io::Read;
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(data));
    for entry in archive.entries()? {
        let mut entry = entry?;
        if entry.path()?.file_name().is_some_and(|n| n == "vanta") {
            let mut out = Vec::new();
            entry.read_to_end(&mut out)?;
            return Ok(out);
        }
    }
    anyhow::bail!("the archive does not contain the vanta binary")
}

fn replace_and_restart(target: &Path, data: &[u8]) -> anyhow::Result<()> {
    let new = target.with_extension("new");
    let old = target.with_extension("old");
    std::fs::write(&new, data)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&new, std::fs::Permissions::from_mode(0o755))?;
    }
    // A running executable can be renamed (Windows) or replaced (Unix).
    let _ = std::fs::remove_file(&old);
    std::fs::rename(target, &old)?;
    if let Err(e) = std::fs::rename(&new, target) {
        let _ = std::fs::rename(&old, target);
        return Err(e.into());
    }
    std::process::Command::new(target)
        .env("VANTA_RESTARTED", "1")
        .spawn()?;
    Ok(())
}

fn cleanup_old_binary() {
    if let Ok(exe) = std::env::current_exe() {
        let _ = std::fs::remove_file(exe.with_extension("old"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_key_parses() {
        assert!(minisign_verify::PublicKey::from_base64(PUBLIC_KEY).is_ok());
    }

    #[test]
    fn rejects_unsigned_data() {
        let bogus = "untrusted comment: x\nRUTbiE3xrAvE+eq5nSz7s2bguvTfttz2FdYywRrRa6DCeSv8VCMZ0kFPAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\ntrusted comment: x\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n";
        assert!(verify(b"payload", bogus).is_err());
    }

    #[test]
    fn asset_names() {
        let v = semver::Version::new(0, 2, 0);
        assert_eq!(
            asset_name(&InstallKind::WindowsInstaller, &v).unwrap(),
            "Vanta-Setup-0.2.0.exe"
        );
        assert_eq!(
            asset_name(&InstallKind::AppImage("/x".into()), &v).unwrap(),
            "Vanta-0.2.0-x86_64.AppImage"
        );
        assert!(asset_name(&InstallKind::PackageManager, &v).is_none());
    }
}
