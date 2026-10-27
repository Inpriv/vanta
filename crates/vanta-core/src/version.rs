//! Version manifest and version JSON models.

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::maven::MavenCoord;
use crate::rules::{Environment, Rule};

pub const MANIFEST_URL: &str = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json";
pub const LIBRARIES_URL: &str = "https://libraries.minecraft.net/";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VersionType {
    Release,
    Snapshot,
    OldBeta,
    OldAlpha,
    #[serde(other)]
    Other,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Latest {
    pub release: String,
    pub snapshot: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ManifestEntry {
    pub id: String,
    #[serde(rename = "type")]
    pub kind: VersionType,
    pub url: String,
    pub release_time: DateTime<Utc>,
    #[serde(default)]
    pub sha1: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct VersionManifest {
    pub latest: Latest,
    pub versions: Vec<ManifestEntry>,
}

impl VersionManifest {
    pub fn get(&self, id: &str) -> Option<&ManifestEntry> {
        self.versions.iter().find(|v| v.id == id)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ArgValue {
    One(String),
    Many(Vec<String>),
}

impl ArgValue {
    pub fn iter(&self) -> impl Iterator<Item = &str> {
        let slice: &[String] = match self {
            ArgValue::One(s) => std::slice::from_ref(s),
            ArgValue::Many(v) => v,
        };
        slice.iter().map(String::as_str)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Argument {
    Plain(String),
    Conditional { rules: Vec<Rule>, value: ArgValue },
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Arguments {
    #[serde(default)]
    pub game: Vec<Argument>,
    #[serde(default)]
    pub jvm: Vec<Argument>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Download {
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub sha1: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub url: String,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct LibraryDownloads {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub artifact: Option<Download>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub classifiers: Option<HashMap<String, Download>>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Extract {
    #[serde(default)]
    pub exclude: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Library {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub downloads: Option<LibraryDownloads>,
    /// Maven repository base used by loader metadata (Fabric, Quilt, old Forge).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sha1: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub natives: Option<HashMap<String, String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub extract: Option<Extract>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rules: Option<Vec<Rule>>,
    /// Legacy Forge: libraries not needed on the client have `clientreq: false`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub clientreq: Option<bool>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AssetIndexRef {
    pub id: String,
    pub sha1: String,
    pub size: u64,
    #[serde(default)]
    pub total_size: Option<u64>,
    pub url: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct JavaVersion {
    #[serde(default = "default_component")]
    pub component: String,
    pub major_version: u32,
}

fn default_component() -> String {
    "jre-legacy".into()
}

impl Default for JavaVersion {
    fn default() -> Self {
        Self {
            component: default_component(),
            major_version: 8,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LoggingFile {
    pub id: String,
    pub sha1: String,
    pub size: u64,
    pub url: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LoggingConfig {
    pub argument: String,
    pub file: LoggingFile,
    #[serde(rename = "type", default)]
    pub kind: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Logging {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client: Option<LoggingConfig>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct VersionDownloads {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client: Option<Download>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VersionJson {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub inherits_from: Option<String>,
    #[serde(rename = "type", default = "default_type")]
    pub kind: VersionType,
    #[serde(default)]
    pub main_class: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minecraft_arguments: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub arguments: Option<Arguments>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub asset_index: Option<AssetIndexRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assets: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub downloads: Option<VersionDownloads>,
    #[serde(default)]
    pub libraries: Vec<Library>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub java_version: Option<JavaVersion>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub logging: Option<Logging>,
    /// Kept as text: loader metadata does not always include a timezone.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub release_time: Option<String>,
    /// Id of the version whose client jar this version runs on (set by merge).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub jar: Option<String>,
}

fn default_type() -> VersionType {
    VersionType::Release
}

impl VersionJson {
    /// Combine a child (loader) version with the parent it inherits from.
    ///
    /// Child values win for scalar fields; child libraries come first so they
    /// take precedence over duplicate parent libraries; argument lists are
    /// concatenated parent-first.
    pub fn merge_onto(self, parent: VersionJson) -> VersionJson {
        let arguments = match (parent.arguments, self.arguments) {
            (Some(p), Some(c)) => Some(Arguments {
                game: p.game.into_iter().chain(c.game).collect(),
                jvm: p.jvm.into_iter().chain(c.jvm).collect(),
            }),
            (p, c) => c.or(p),
        };
        let mut libraries = self.libraries;
        libraries.extend(parent.libraries);
        VersionJson {
            id: self.id,
            inherits_from: parent.inherits_from,
            kind: self.kind,
            main_class: if self.main_class.is_empty() {
                parent.main_class
            } else {
                self.main_class
            },
            minecraft_arguments: self.minecraft_arguments.or(parent.minecraft_arguments),
            arguments,
            asset_index: self.asset_index.or(parent.asset_index),
            assets: self.assets.or(parent.assets),
            downloads: match self.downloads {
                Some(d) if d.client.is_some() => Some(d),
                _ => parent.downloads,
            },
            libraries: dedup_libraries(libraries),
            java_version: self.java_version.or(parent.java_version),
            logging: self.logging.or(parent.logging),
            release_time: self.release_time.or(parent.release_time),
            jar: self.jar.or(parent.jar).or(Some(parent.id)),
        }
    }

    /// The version id whose `.jar` holds the game client.
    pub fn jar_id(&self) -> &str {
        self.jar.as_deref().unwrap_or(&self.id)
    }

    pub fn java(&self) -> JavaVersion {
        self.java_version.clone().unwrap_or_default()
    }

    pub fn asset_index_id(&self) -> &str {
        self.asset_index
            .as_ref()
            .map(|a| a.id.as_str())
            .or(self.assets.as_deref())
            .unwrap_or("legacy")
    }
}

/// Keep the first library for every `group:artifact[:classifier]` key.
fn dedup_libraries(libraries: Vec<Library>) -> Vec<Library> {
    let mut seen = HashSet::new();
    libraries
        .into_iter()
        .filter(|lib| {
            // Platform-specific variants share a key but differ in rules, so
            // libraries with rules are never treated as duplicates.
            if lib.rules.is_some() || lib.natives.is_some() {
                return true;
            }
            match lib.name.parse::<MavenCoord>() {
                Ok(c) => seen.insert(c.key()),
                Err(_) => true,
            }
        })
        .collect()
}

/// A library file resolved for the current platform.
#[derive(Clone, Debug, PartialEq)]
pub struct ResolvedLibrary {
    /// Path relative to the libraries directory, `/`-separated.
    pub path: String,
    /// Empty when the file is produced locally (e.g. by a Forge installer).
    pub url: String,
    pub sha1: Option<String>,
    pub size: Option<u64>,
    /// `Some` for native archives that must be extracted rather than put on
    /// the classpath; contains the exclusion prefixes.
    pub extract: Option<Vec<String>>,
}

fn join_url(base: &str, path: &str) -> String {
    if base.ends_with('/') {
        format!("{base}{path}")
    } else {
        format!("{base}/{path}")
    }
}

impl Library {
    /// All files this library contributes on the given platform.
    pub fn resolve(&self, env: &Environment) -> Vec<ResolvedLibrary> {
        if !env.allows(self.rules.as_deref()) || self.clientreq == Some(false) {
            return Vec::new();
        }
        let Ok(coord) = self.name.parse::<MavenCoord>() else {
            return Vec::new();
        };
        let mut out = Vec::new();
        let base = self.url.as_deref().unwrap_or(LIBRARIES_URL);

        let has_natives = self.natives.is_some();
        let artifact = self.downloads.as_ref().and_then(|d| d.artifact.as_ref());
        match artifact {
            Some(a) => out.push(ResolvedLibrary {
                path: a.path.clone().unwrap_or_else(|| coord.path()),
                url: a.url.clone(),
                sha1: a.sha1.clone(),
                size: a.size,
                extract: None,
            }),
            // Old natives-only libraries (e.g. lwjgl-platform) have no main jar.
            None if has_natives && self.downloads.is_some() => {}
            None => out.push(ResolvedLibrary {
                path: coord.path(),
                url: join_url(base, &coord.path()),
                sha1: self.sha1.clone(),
                size: self.size,
                extract: None,
            }),
        }

        if let Some(natives) = &self.natives
            && let Some(classifier) = natives.get(env.os_name)
        {
            let classifier = classifier.replace("${arch}", env.arch_bits());
            let exclude = self
                .extract
                .as_ref()
                .map(|e| e.exclude.clone())
                .unwrap_or_default();
            let native = self
                .downloads
                .as_ref()
                .and_then(|d| d.classifiers.as_ref())
                .and_then(|c| c.get(&classifier));
            let coord = coord.with_classifier(&classifier);
            out.push(match native {
                Some(a) => ResolvedLibrary {
                    path: a.path.clone().unwrap_or_else(|| coord.path()),
                    url: a.url.clone(),
                    sha1: a.sha1.clone(),
                    size: a.size,
                    extract: Some(exclude),
                },
                None => ResolvedLibrary {
                    path: coord.path(),
                    url: join_url(base, &coord.path()),
                    sha1: None,
                    size: None,
                    extract: Some(exclude),
                },
            });
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rules::Features;

    fn linux() -> Environment {
        Environment {
            os_name: "linux",
            arch: "x86_64",
            os_version: String::new(),
            features: Features::default(),
        }
    }

    fn windows() -> Environment {
        Environment {
            os_name: "windows",
            ..linux()
        }
    }

    const VANILLA: &str = r#"{
        "id": "1.20.1", "type": "release", "mainClass": "net.minecraft.client.main.Main",
        "assetIndex": {"id": "5", "sha1": "abc", "size": 1, "totalSize": 2, "url": "https://x/5.json"},
        "assets": "5",
        "downloads": {"client": {"sha1": "c0ffee", "size": 10, "url": "https://x/client.jar"}},
        "javaVersion": {"component": "java-runtime-gamma", "majorVersion": 17},
        "arguments": {"game": ["--username", "${auth_player_name}"], "jvm": ["-cp", "${classpath}"]},
        "libraries": [
            {"name": "org.ow2.asm:asm:9.3", "downloads": {"artifact": {"path": "org/ow2/asm/asm/9.3/asm-9.3.jar", "sha1": "1", "size": 1, "url": "https://libraries.minecraft.net/org/ow2/asm/asm/9.3/asm-9.3.jar"}}},
            {"name": "org.lwjgl:lwjgl:3.3.1:natives-windows", "rules": [{"action": "allow", "os": {"name": "windows"}}],
             "downloads": {"artifact": {"path": "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-windows.jar", "sha1": "2", "size": 1, "url": "https://l/w.jar"}}}
        ]
    }"#;

    const FABRIC: &str = r#"{
        "id": "fabric-loader-0.15.0-1.20.1", "inheritsFrom": "1.20.1", "type": "release",
        "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
        "arguments": {"game": [], "jvm": ["-DFabricMcEmu= net.minecraft.client.main.Main "]},
        "libraries": [
            {"name": "org.ow2.asm:asm:9.6", "url": "https://maven.fabricmc.net/", "sha1": "aa", "size": 5},
            {"name": "net.fabricmc:fabric-loader:0.15.0", "url": "https://maven.fabricmc.net/"}
        ]
    }"#;

    #[test]
    fn parses_vanilla() {
        let v: VersionJson = serde_json::from_str(VANILLA).unwrap();
        assert_eq!(v.java().major_version, 17);
        assert_eq!(v.asset_index_id(), "5");
        assert_eq!(v.libraries.len(), 2);
    }

    #[test]
    fn merges_fabric_over_vanilla() {
        let parent: VersionJson = serde_json::from_str(VANILLA).unwrap();
        let child: VersionJson = serde_json::from_str(FABRIC).unwrap();
        let merged = child.merge_onto(parent);
        assert_eq!(merged.id, "fabric-loader-0.15.0-1.20.1");
        assert_eq!(
            merged.main_class,
            "net.fabricmc.loader.impl.launch.knot.KnotClient"
        );
        assert_eq!(merged.jar_id(), "1.20.1");
        assert_eq!(merged.java().major_version, 17);
        assert!(merged.inherits_from.is_none());
        // Fabric's newer ASM replaces vanilla's.
        let asm: Vec<_> = merged
            .libraries
            .iter()
            .filter(|l| l.name.starts_with("org.ow2.asm:asm:"))
            .collect();
        assert_eq!(asm.len(), 1);
        assert_eq!(asm[0].name, "org.ow2.asm:asm:9.6");
        let args = merged.arguments.unwrap();
        assert_eq!(args.game.len(), 2);
        assert_eq!(args.jvm.len(), 3);
    }

    #[test]
    fn resolves_libraries_per_platform() {
        let v: VersionJson = serde_json::from_str(VANILLA).unwrap();
        let on = |env: &Environment| -> Vec<String> {
            v.libraries
                .iter()
                .flat_map(|l| l.resolve(env))
                .map(|r| r.path)
                .collect()
        };
        assert_eq!(on(&linux()), vec!["org/ow2/asm/asm/9.3/asm-9.3.jar"]);
        assert_eq!(on(&windows()).len(), 2);
    }

    #[test]
    fn resolves_maven_url_libraries() {
        let child: VersionJson = serde_json::from_str(FABRIC).unwrap();
        let r = child.libraries[1].resolve(&linux());
        assert_eq!(
            r[0].url,
            "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.15.0/fabric-loader-0.15.0.jar"
        );
    }

    #[test]
    fn resolves_legacy_natives() {
        let lib: Library = serde_json::from_str(
            r#"{"name": "org.lwjgl.lwjgl:lwjgl-platform:2.9.4",
                "natives": {"linux": "natives-linux", "windows": "natives-windows-${arch}"},
                "extract": {"exclude": ["META-INF/"]},
                "downloads": {"classifiers": {
                    "natives-linux": {"path": "l.jar", "sha1": "1", "size": 1, "url": "https://x/l.jar"},
                    "natives-windows-64": {"path": "w64.jar", "sha1": "2", "size": 1, "url": "https://x/w64.jar"}
                }}}"#,
        )
        .unwrap();
        let r = lib.resolve(&windows());
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].path, "w64.jar");
        assert_eq!(r[0].extract.as_deref(), Some(&["META-INF/".to_owned()][..]));
        assert_eq!(lib.resolve(&linux())[0].path, "l.jar");
    }

    #[test]
    fn legacy_forge_clientreq() {
        let lib: Library = serde_json::from_str(r#"{"name": "a:b:1", "clientreq": false}"#).unwrap();
        assert!(lib.resolve(&linux()).is_empty());
    }
}
