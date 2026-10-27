//! Mods, resource packs and shader packs inside an instance.
//!
//! Files are downloaded once into the shared content store and hard-linked
//! into each instance that uses them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::download::{DownloadTask, Downloader, Progress};
use crate::error::{Error, Result};
use crate::paths::{self, Paths};

pub const DISABLED_SUFFIX: &str = ".disabled";
const INDEX_FILE: &str = "content.json";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ContentKind {
    Mod,
    ResourcePack,
    ShaderPack,
}

impl ContentKind {
    pub const ALL: [ContentKind; 3] = [
        ContentKind::Mod,
        ContentKind::ResourcePack,
        ContentKind::ShaderPack,
    ];

    pub fn dir_name(self) -> &'static str {
        match self {
            Self::Mod => "mods",
            Self::ResourcePack => "resourcepacks",
            Self::ShaderPack => "shaderpacks",
        }
    }

    /// Modrinth `project_type`.
    pub fn modrinth_type(self) -> &'static str {
        match self {
            Self::Mod => "mod",
            Self::ResourcePack => "resourcepack",
            Self::ShaderPack => "shader",
        }
    }

    pub fn from_index(i: usize) -> Self {
        Self::ALL.get(i).copied().unwrap_or(Self::Mod)
    }
}

/// What Vanta knows about a file it installed from Modrinth.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ContentMeta {
    pub project_id: String,
    pub version_id: String,
    pub title: String,
    pub version_number: String,
    #[serde(default)]
    pub icon_url: Option<String>,
    pub sha1: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ContentEntry {
    pub kind: ContentKind,
    /// File name without the `.disabled` suffix.
    pub file_name: String,
    pub path: PathBuf,
    pub enabled: bool,
    pub size: u64,
    pub meta: Option<ContentMeta>,
}

impl ContentEntry {
    pub fn display_name(&self) -> &str {
        self.meta
            .as_ref()
            .map(|m| m.title.as_str())
            .unwrap_or(&self.file_name)
    }
}

#[derive(Default, Serialize, Deserialize)]
struct Index {
    /// Keyed by `<dir>/<file name>` (without `.disabled`).
    files: BTreeMap<String, ContentMeta>,
}

fn index_path(instance_dir: &Path) -> PathBuf {
    instance_dir.join(INDEX_FILE)
}

fn load_index(instance_dir: &Path) -> Index {
    std::fs::read(index_path(instance_dir))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

fn save_index(instance_dir: &Path, index: &Index) -> Result<()> {
    std::fs::write(index_path(instance_dir), serde_json::to_vec_pretty(index)?)?;
    Ok(())
}

fn key(kind: ContentKind, file_name: &str) -> String {
    format!("{}/{}", kind.dir_name(), file_name)
}

/// Reject names that could escape the content directory.
pub fn safe_file_name(name: &str) -> Result<&str> {
    if name.is_empty()
        || name.contains(['/', '\\', '\0'])
        || name == "."
        || name == ".."
        || name.contains(':')
    {
        return Err(Error::Instance(format!("invalid file name `{name}`")));
    }
    Ok(name)
}

/// The installed content of an instance.
pub struct Content<'a> {
    pub instance_dir: &'a Path,
    pub game_dir: &'a Path,
}

impl Content<'_> {
    pub fn list(&self, kind: ContentKind) -> Vec<ContentEntry> {
        let index = load_index(self.instance_dir);
        let Ok(rd) = std::fs::read_dir(self.game_dir.join(kind.dir_name())) else {
            return Vec::new();
        };
        let mut out: Vec<ContentEntry> = rd
            .flatten()
            .filter(|e| e.file_type().map(|t| t.is_file() || t.is_dir()).unwrap_or(false))
            .filter_map(|e| {
                let raw = e.file_name().to_string_lossy().into_owned();
                if raw.starts_with('.') || raw.ends_with(".part") {
                    return None;
                }
                let (file_name, enabled) = match raw.strip_suffix(DISABLED_SUFFIX) {
                    Some(n) => (n.to_owned(), false),
                    None => (raw.clone(), true),
                };
                if kind == ContentKind::Mod && !file_name.ends_with(".jar") && !file_name.ends_with(".zip") {
                    return None;
                }
                Some(ContentEntry {
                    kind,
                    meta: index.files.get(&key(kind, &file_name)).cloned(),
                    size: e.metadata().map(|m| m.len()).unwrap_or(0),
                    path: e.path(),
                    file_name,
                    enabled,
                })
            })
            .collect();
        out.sort_by_key(|e| e.display_name().to_lowercase());
        out
    }

    pub fn set_enabled(&self, entry: &ContentEntry, enabled: bool) -> Result<()> {
        if entry.enabled == enabled {
            return Ok(());
        }
        let dir = self.game_dir.join(entry.kind.dir_name());
        let target = if enabled {
            dir.join(&entry.file_name)
        } else {
            dir.join(format!("{}{DISABLED_SUFFIX}", entry.file_name))
        };
        std::fs::rename(&entry.path, &target).map_err(Error::io_at(&entry.path))
    }

    pub fn remove(&self, entry: &ContentEntry) -> Result<()> {
        if entry.path.is_dir() {
            std::fs::remove_dir_all(&entry.path)?;
        } else {
            std::fs::remove_file(&entry.path).map_err(Error::io_at(&entry.path))?;
        }
        let mut index = load_index(self.instance_dir);
        index.files.remove(&key(entry.kind, &entry.file_name));
        save_index(self.instance_dir, &index)
    }

    /// Download a file into the shared store and link it into the instance.
    /// `replaces` is an older file of the same project to remove (updates).
    #[allow(clippy::too_many_arguments)]
    pub async fn install(
        &self,
        paths: &Paths,
        downloader: &Downloader,
        kind: ContentKind,
        url: &str,
        file_name: &str,
        size: Option<u64>,
        meta: ContentMeta,
        progress: &Progress,
    ) -> Result<PathBuf> {
        let file_name = safe_file_name(file_name)?;
        let stored = paths.content_store(&meta.sha1);
        downloader
            .download_all(
                vec![
                    DownloadTask::new(url, &stored)
                        .sha1(Some(meta.sha1.clone()))
                        .size(size),
                ],
                progress,
            )
            .await?;
        let dest = self.game_dir.join(kind.dir_name()).join(file_name);
        paths::link_or_copy(&stored, &dest)?;

        let mut index = load_index(self.instance_dir);
        // Drop older files of the same project.
        let stale: Vec<String> = index
            .files
            .iter()
            .filter(|(k, m)| m.project_id == meta.project_id && **k != key(kind, file_name))
            .map(|(k, _)| k.clone())
            .collect();
        for k in stale {
            index.files.remove(&k);
            let old = self.game_dir.join(&k);
            let _ = std::fs::remove_file(&old);
            let _ = std::fs::remove_file(old.with_file_name(format!(
                "{}{DISABLED_SUFFIX}",
                old.file_name().unwrap_or_default().to_string_lossy()
            )));
        }
        index.files.insert(key(kind, file_name), meta);
        save_index(self.instance_dir, &index)?;
        Ok(dest)
    }

    /// Record metadata for a file that was placed by other means (mrpack).
    pub fn record(&self, kind: ContentKind, file_name: &str, meta: ContentMeta) -> Result<()> {
        let mut index = load_index(self.instance_dir);
        index.files.insert(key(kind, file_name), meta);
        save_index(self.instance_dir, &index)
    }

    /// Project ids of everything installed from Modrinth.
    pub fn installed_projects(&self) -> BTreeMap<String, ContentMeta> {
        load_index(self.instance_dir)
            .files
            .into_values()
            .map(|m| (m.project_id.clone(), m))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn meta(project: &str, sha1: &str) -> ContentMeta {
        ContentMeta {
            project_id: project.into(),
            version_id: "v".into(),
            title: "Sodium".into(),
            version_number: "1".into(),
            icon_url: None,
            sha1: sha1.into(),
        }
    }

    #[test]
    fn list_toggle_remove() {
        let dir = tempfile::tempdir().unwrap();
        let game = dir.path().join("minecraft");
        std::fs::create_dir_all(game.join("mods")).unwrap();
        std::fs::write(game.join("mods/sodium.jar"), b"jar").unwrap();
        std::fs::write(game.join("mods/notes.txt"), b"ignored").unwrap();
        let c = Content {
            instance_dir: dir.path(),
            game_dir: &game,
        };
        c.record(ContentKind::Mod, "sodium.jar", meta("AANobbMI", "x"))
            .unwrap();

        let list = c.list(ContentKind::Mod);
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].display_name(), "Sodium");
        assert!(list[0].enabled);

        c.set_enabled(&list[0], false).unwrap();
        assert!(game.join("mods/sodium.jar.disabled").exists());
        let list = c.list(ContentKind::Mod);
        assert!(!list[0].enabled);
        assert_eq!(list[0].file_name, "sodium.jar");
        assert!(list[0].meta.is_some(), "metadata survives disabling");

        c.remove(&list[0]).unwrap();
        assert!(c.list(ContentKind::Mod).is_empty());
        assert!(c.installed_projects().is_empty());
    }

    #[test]
    fn rejects_unsafe_names() {
        for bad in ["../x.jar", "a/b.jar", "..", "", "C:evil.jar", "a\\b.jar"] {
            assert!(safe_file_name(bad).is_err(), "{bad}");
        }
        assert!(safe_file_name("sodium-0.5.jar").is_ok());
    }
}
