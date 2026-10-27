//! On-disk layout of the Vanta data directory.
//!
//! Everything that can be shared between instances (version metadata, client
//! jars, libraries, assets, Java runtimes, mod files) lives once under the data
//! root. Instances only hold their own game directory and settings.

use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct Paths {
    root: PathBuf,
}

impl Paths {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// Platform default: `~/.local/share/vanta` on Linux, `%APPDATA%\Vanta` on
    /// Windows, `~/Library/Application Support/Vanta` on macOS.
    pub fn default_root() -> PathBuf {
        let base = dirs::data_dir().unwrap_or_else(|| PathBuf::from("."));
        if cfg!(target_os = "linux") {
            base.join("vanta")
        } else {
            base.join("Vanta")
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn versions_dir(&self) -> PathBuf {
        self.root.join("versions")
    }

    pub fn version_json(&self, id: &str) -> PathBuf {
        self.versions_dir().join(id).join(format!("{id}.json"))
    }

    pub fn version_jar(&self, id: &str) -> PathBuf {
        self.versions_dir().join(id).join(format!("{id}.jar"))
    }

    pub fn manifest_cache(&self) -> PathBuf {
        self.versions_dir().join("version_manifest_v2.json")
    }

    pub fn libraries_dir(&self) -> PathBuf {
        self.root.join("libraries")
    }

    pub fn assets_dir(&self) -> PathBuf {
        self.root.join("assets")
    }

    pub fn asset_index(&self, id: &str) -> PathBuf {
        self.assets_dir().join("indexes").join(format!("{id}.json"))
    }

    pub fn asset_object(&self, hash: &str) -> PathBuf {
        self.assets_dir().join("objects").join(&hash[..2]).join(hash)
    }

    pub fn virtual_assets(&self, index_id: &str) -> PathBuf {
        self.assets_dir().join("virtual").join(index_id)
    }

    pub fn log_configs_dir(&self) -> PathBuf {
        self.assets_dir().join("log_configs")
    }

    pub fn natives_dir(&self, version_id: &str) -> PathBuf {
        self.root.join("natives").join(version_id)
    }

    pub fn runtimes_dir(&self) -> PathBuf {
        self.root.join("runtimes")
    }

    pub fn instances_dir(&self) -> PathBuf {
        self.root.join("instances")
    }

    pub fn cache_dir(&self) -> PathBuf {
        self.root.join("cache")
    }

    /// Content-addressed store for mod, resource pack and shader files.
    pub fn content_store(&self, sha1: &str) -> PathBuf {
        self.cache_dir()
            .join("content")
            .join(&sha1[..2.min(sha1.len())])
            .join(sha1)
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.root.join("logs")
    }

    pub fn settings_file(&self) -> PathBuf {
        self.root.join("settings.json")
    }

    pub fn accounts_file(&self) -> PathBuf {
        self.root.join("accounts.json")
    }

    pub fn temp_dir(&self) -> PathBuf {
        self.root.join("tmp")
    }
}

/// Hard-link `src` to `dst`, falling back to a copy (e.g. across filesystems).
pub fn link_or_copy(src: &Path, dst: &Path) -> std::io::Result<()> {
    if let Some(parent) = dst.parent() {
        std::fs::create_dir_all(parent)?;
    }
    if dst.exists() {
        std::fs::remove_file(dst)?;
    }
    if std::fs::hard_link(src, dst).is_err() {
        std::fs::copy(src, dst)?;
    }
    Ok(())
}
