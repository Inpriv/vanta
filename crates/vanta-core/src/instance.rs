//! Instances: isolated game directories with their own version, loader and
//! settings.

use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LoaderKind {
    #[default]
    Vanilla,
    Fabric,
    Quilt,
    Forge,
    NeoForge,
}

impl LoaderKind {
    pub const ALL: [LoaderKind; 5] = [
        LoaderKind::Vanilla,
        LoaderKind::Fabric,
        LoaderKind::Quilt,
        LoaderKind::Forge,
        LoaderKind::NeoForge,
    ];

    pub fn display_name(self) -> &'static str {
        match self {
            Self::Vanilla => "Vanilla",
            Self::Fabric => "Fabric",
            Self::Quilt => "Quilt",
            Self::Forge => "Forge",
            Self::NeoForge => "NeoForge",
        }
    }

    /// Identifier used by Modrinth (`loaders` facet).
    pub fn modrinth_id(self) -> Option<&'static str> {
        match self {
            Self::Vanilla => None,
            Self::Fabric => Some("fabric"),
            Self::Quilt => Some("quilt"),
            Self::Forge => Some("forge"),
            Self::NeoForge => Some("neoforge"),
        }
    }

    /// Key used in `.mrpack` `dependencies`.
    pub fn mrpack_key(self) -> Option<&'static str> {
        match self {
            Self::Vanilla => None,
            Self::Fabric => Some("fabric-loader"),
            Self::Quilt => Some("quilt-loader"),
            Self::Forge => Some("forge"),
            Self::NeoForge => Some("neoforge"),
        }
    }

    pub fn from_index(i: usize) -> Self {
        Self::ALL.get(i).copied().unwrap_or_default()
    }

    pub fn index(self) -> usize {
        Self::ALL.iter().position(|l| *l == self).unwrap_or(0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Resolution {
    pub width: u32,
    pub height: u32,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Instance {
    /// Directory name; not stored in the file.
    #[serde(skip)]
    pub id: String,
    pub name: String,
    /// Built-in icon key (`grass`, `stone`, ...) or a file name in the instance dir.
    #[serde(default = "default_icon")]
    pub icon: String,
    pub game_version: String,
    #[serde(default)]
    pub loader: LoaderKind,
    #[serde(default)]
    pub loader_version: Option<String>,
    /// Version id of the installed loader profile (cached after install).
    #[serde(default)]
    pub installed_version_id: Option<String>,
    #[serde(default)]
    pub memory_mb: Option<u32>,
    #[serde(default)]
    pub jvm_args: String,
    #[serde(default)]
    pub resolution: Option<Resolution>,
    #[serde(default)]
    pub java_path: Option<PathBuf>,
    pub created: DateTime<Utc>,
    #[serde(default)]
    pub last_played: Option<DateTime<Utc>>,
    #[serde(default)]
    pub play_time_secs: u64,
}

fn default_icon() -> String {
    "grass".into()
}

impl Instance {
    pub fn new(name: impl Into<String>, game_version: impl Into<String>) -> Self {
        Self {
            id: String::new(),
            name: name.into(),
            icon: default_icon(),
            game_version: game_version.into(),
            loader: LoaderKind::Vanilla,
            loader_version: None,
            installed_version_id: None,
            memory_mb: None,
            jvm_args: String::new(),
            resolution: None,
            java_path: None,
            created: Utc::now(),
            last_played: None,
            play_time_secs: 0,
        }
    }

    /// Extra JVM arguments split shell-style on whitespace (quotes respected).
    pub fn jvm_args_list(&self) -> Vec<String> {
        split_args(&self.jvm_args)
    }
}

pub fn split_args(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    let mut has = false;
    for c in s.chars() {
        match (quote, c) {
            (Some(q), c) if c == q => quote = None,
            (None, '"' | '\'') => {
                quote = Some(c);
                has = true;
            }
            (None, c) if c.is_whitespace() => {
                if has {
                    out.push(std::mem::take(&mut cur));
                    has = false;
                }
            }
            (_, c) => {
                cur.push(c);
                has = true;
            }
        }
    }
    if has {
        out.push(cur);
    }
    out
}

/// Turn a display name into a safe directory name.
pub fn slugify(name: &str) -> String {
    let mut slug = String::new();
    for c in name.trim().chars() {
        if c.is_ascii_alphanumeric() {
            slug.push(c.to_ascii_lowercase());
        } else if (c == ' ' || c == '-' || c == '_' || c == '.') && !slug.ends_with('-') {
            slug.push('-');
        }
    }
    let slug = slug.trim_matches('-').to_owned();
    if slug.is_empty() { "instance".into() } else { slug }
}

const INSTANCE_FILE: &str = "instance.json";

#[derive(Clone, Debug)]
pub struct InstanceStore {
    root: PathBuf,
}

impl InstanceStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn dir(&self, id: &str) -> PathBuf {
        self.root.join(id)
    }

    /// The `.minecraft`-equivalent game directory.
    pub fn game_dir(&self, id: &str) -> PathBuf {
        self.dir(id).join("minecraft")
    }

    pub fn list(&self) -> Vec<Instance> {
        let Ok(rd) = std::fs::read_dir(&self.root) else {
            return Vec::new();
        };
        let mut all: Vec<Instance> = rd
            .flatten()
            .filter_map(|e| {
                let id = e.file_name().to_string_lossy().into_owned();
                self.get(&id).ok()
            })
            .collect();
        all.sort_by(|a, b| {
            b.last_played
                .cmp(&a.last_played)
                .then_with(|| b.created.cmp(&a.created))
        });
        all
    }

    pub fn get(&self, id: &str) -> Result<Instance> {
        let path = self.dir(id).join(INSTANCE_FILE);
        let bytes = std::fs::read(&path).map_err(Error::io_at(&path))?;
        let mut inst: Instance = serde_json::from_slice(&bytes)?;
        inst.id = id.to_owned();
        Ok(inst)
    }

    pub fn save(&self, inst: &Instance) -> Result<()> {
        if inst.id.is_empty() {
            return Err(Error::Instance("instance has no id".into()));
        }
        let dir = self.dir(&inst.id);
        std::fs::create_dir_all(&dir).map_err(Error::io_at(&dir))?;
        let tmp = dir.join("instance.json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(inst)?)?;
        std::fs::rename(&tmp, dir.join(INSTANCE_FILE))?;
        Ok(())
    }

    fn unique_id(&self, name: &str) -> String {
        let base = slugify(name);
        let mut id = base.clone();
        let mut n = 2;
        while self.dir(&id).exists() {
            id = format!("{base}-{n}");
            n += 1;
        }
        id
    }

    pub fn create(&self, mut inst: Instance) -> Result<Instance> {
        inst.id = self.unique_id(&inst.name);
        self.save(&inst)?;
        std::fs::create_dir_all(self.game_dir(&inst.id))?;
        Ok(inst)
    }

    pub fn rename(&self, id: &str, name: &str) -> Result<Instance> {
        let name = name.trim();
        if name.is_empty() {
            return Err(Error::Instance("name cannot be empty".into()));
        }
        let mut inst = self.get(id)?;
        inst.name = name.to_owned();
        self.save(&inst)?;
        Ok(inst)
    }

    /// Copy an instance including its game directory.
    pub fn duplicate(&self, id: &str, name: &str) -> Result<Instance> {
        let mut inst = self.get(id)?;
        inst.name = name.to_owned();
        inst.created = Utc::now();
        inst.last_played = None;
        inst.play_time_secs = 0;
        inst.id = self.unique_id(name);
        copy_dir(&self.dir(id), &self.dir(&inst.id))?;
        self.save(&inst)?;
        Ok(inst)
    }

    pub fn delete(&self, id: &str) -> Result<()> {
        if id.is_empty() || id.contains(['/', '\\']) || id == ".." {
            return Err(Error::Instance(format!("refusing to delete `{id}`")));
        }
        let dir = self.dir(id);
        std::fs::remove_dir_all(&dir).map_err(Error::io_at(&dir))
    }
}

fn copy_dir(src: &Path, dst: &Path) -> Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let target = dst.join(entry.file_name());
        let ty = entry.file_type()?;
        if ty.is_dir() {
            copy_dir(&entry.path(), &target)?;
        } else if ty.is_file() {
            std::fs::copy(entry.path(), &target)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs() {
        assert_eq!(slugify("My Cool Pack!"), "my-cool-pack");
        assert_eq!(slugify("  ---  "), "instance");
        assert_eq!(slugify("1.20.1 Fabric"), "1-20-1-fabric");
    }

    #[test]
    fn splits_jvm_args() {
        assert_eq!(
            split_args(r#"-Xss2M  -Dfoo="a b" -XX:+UseG1GC"#),
            vec!["-Xss2M", "-Dfoo=a b", "-XX:+UseG1GC"]
        );
        assert!(split_args("   ").is_empty());
    }

    #[test]
    fn lifecycle() {
        let dir = tempfile::tempdir().unwrap();
        let store = InstanceStore::new(dir.path());
        let a = store.create(Instance::new("Survival", "1.21.1")).unwrap();
        let b = store.create(Instance::new("Survival", "1.21.1")).unwrap();
        assert_eq!(a.id, "survival");
        assert_eq!(b.id, "survival-2");

        std::fs::write(store.game_dir(&a.id).join("options.txt"), "fov:90").unwrap();
        let c = store.duplicate(&a.id, "Survival copy").unwrap();
        assert_eq!(
            std::fs::read_to_string(store.game_dir(&c.id).join("options.txt")).unwrap(),
            "fov:90"
        );

        let renamed = store.rename(&a.id, "Hardcore").unwrap();
        assert_eq!(renamed.id, "survival");
        assert_eq!(store.get("survival").unwrap().name, "Hardcore");
        assert!(store.rename(&a.id, "  ").is_err());

        assert_eq!(store.list().len(), 3);
        store.delete(&b.id).unwrap();
        assert_eq!(store.list().len(), 2);
        assert!(store.delete("..").is_err());
    }
}
