//! Forge and NeoForge, installed by running their official installer's
//! client-side processors (modern) or by unpacking the universal jar (legacy).

use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use super::LoaderVersion;
use crate::download::{self, DownloadTask, Downloader, Progress};
use crate::error::{Error, Result};
use crate::install::Installer;
use crate::java;
use crate::maven::MavenCoord;
use crate::meta::Meta;
use crate::rules::Environment;
use crate::version::{Library, VersionJson};

pub const FORGE_MAVEN: &str = "https://maven.minecraftforge.net/";
pub const NEOFORGE_MAVEN: &str = "https://maven.neoforged.net/releases/";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavor {
    Forge,
    NeoForge,
}

impl Flavor {
    fn installer_url(self, game_version: &str, loader_version: &str) -> String {
        match self {
            Flavor::Forge => {
                let full = format!("{game_version}-{loader_version}");
                format!("{FORGE_MAVEN}net/minecraftforge/forge/{full}/forge-{full}-installer.jar")
            }
            Flavor::NeoForge => format!(
                "{NEOFORGE_MAVEN}net/neoforged/neoforge/{loader_version}/neoforge-{loader_version}-installer.jar"
            ),
        }
    }
}

/// Minecraft version a NeoForge version targets: `21.1.77` → `1.21.1`,
/// `20.4.237` → `1.20.4`, `21.0.1-beta` → `1.21`; year-based releases
/// (`26.1.0.1`) map to `26.1`.
pub fn neoforge_game_version(v: &str) -> Option<String> {
    let core = v.split(['-', '+']).next()?;
    let parts: Vec<u32> = core.split('.').map(|p| p.parse().ok()).collect::<Option<_>>()?;
    let (major, minor) = (*parts.first()?, *parts.get(1)?);
    if major >= 26 {
        let patch = parts.get(2).copied().unwrap_or(0);
        return Some(if patch == 0 {
            format!("{major}.{minor}")
        } else {
            format!("{major}.{minor}.{patch}")
        });
    }
    Some(if minor == 0 {
        format!("1.{major}")
    } else {
        format!("1.{major}.{minor}")
    })
}

pub async fn versions(meta: &Meta, flavor: Flavor, game_version: &str) -> Result<Vec<LoaderVersion>> {
    match flavor {
        Flavor::Forge => {
            let all: HashMap<String, Vec<String>> = download::fetch_json(
                &meta.client,
                "https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json",
            )
            .await?;
            #[derive(Deserialize)]
            struct Promos {
                promos: HashMap<String, String>,
            }
            let promos = download::fetch_json::<Promos>(
                &meta.client,
                "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json",
            )
            .await
            .map(|p| p.promos)
            .unwrap_or_default();
            let recommended = promos.get(&format!("{game_version}-recommended"));
            let prefix = format!("{game_version}-");
            Ok(all
                .get(game_version)
                .into_iter()
                .flatten()
                .rev()
                .filter_map(|full| full.strip_prefix(&prefix))
                // Branch-suffixed builds like `-1.7.10` are part of the version.
                .map(|v| LoaderVersion {
                    stable: Some(&v.to_owned()) == recommended,
                    version: v.to_owned(),
                })
                .collect())
        }
        Flavor::NeoForge => {
            #[derive(Deserialize)]
            struct List {
                versions: Vec<String>,
            }
            let list: List = download::fetch_json(
                &meta.client,
                "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge",
            )
            .await?;
            Ok(list
                .versions
                .into_iter()
                .rev()
                .filter(|v| neoforge_game_version(v).as_deref() == Some(game_version))
                .map(|v| LoaderVersion {
                    stable: !v.contains("beta") && !v.contains("alpha"),
                    version: v,
                })
                .collect())
        }
    }
}

#[derive(Deserialize)]
struct DataEntry {
    client: String,
}

#[derive(Deserialize)]
struct Processor {
    jar: String,
    #[serde(default)]
    classpath: Vec<String>,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    sides: Option<Vec<String>>,
    #[serde(default)]
    outputs: HashMap<String, String>,
}

#[derive(Deserialize)]
struct ModernProfile {
    #[serde(default)]
    data: HashMap<String, DataEntry>,
    #[serde(default)]
    processors: Vec<Processor>,
    #[serde(default)]
    libraries: Vec<Library>,
    #[serde(default = "default_json")]
    json: String,
}

fn default_json() -> String {
    "/version.json".into()
}

#[derive(Deserialize)]
struct LegacyInstall {
    path: String,
    #[serde(rename = "filePath")]
    file_path: String,
    minecraft: String,
}

#[derive(Deserialize)]
struct LegacyProfile {
    install: LegacyInstall,
    #[serde(rename = "versionInfo")]
    version_info: VersionJson,
}

pub struct ForgeContext<'a> {
    pub meta: &'a Meta,
    pub downloader: &'a Downloader,
    pub env: &'a Environment,
}

fn read_entry(zip: &mut zip::ZipArchive<std::fs::File>, name: &str) -> Result<Vec<u8>> {
    let name = name.trim_start_matches('/');
    let mut entry = zip.by_name(name)?;
    let mut buf = Vec::with_capacity(entry.size() as usize);
    entry.read_to_end(&mut buf)?;
    Ok(buf)
}

/// Install Forge or NeoForge and return the resulting version id.
pub async fn install(
    ctx: &ForgeContext<'_>,
    flavor: Flavor,
    game_version: &str,
    loader_version: &str,
    progress: &Progress,
) -> Result<String> {
    let paths = &ctx.meta.paths;
    let installer = paths
        .cache_dir()
        .join("installers")
        .join(format!("{flavor:?}-{game_version}-{loader_version}-installer.jar").to_lowercase());
    ctx.downloader
        .fetch(
            &DownloadTask::new(flavor.installer_url(game_version, loader_version), &installer),
            &Progress::default(),
        )
        .await
        .map_err(|e| Error::Loader(format!("could not download the installer: {e}")))?;

    let mut zip = zip::ZipArchive::new(std::fs::File::open(&installer).map_err(Error::io_at(&installer))?)?;
    let profile_bytes = read_entry(&mut zip, "install_profile.json")?;
    let profile_value: serde_json::Value = serde_json::from_slice(&profile_bytes)?;

    if profile_value.get("install").is_some() {
        let legacy: LegacyProfile = serde_json::from_value(profile_value)?;
        return install_legacy(ctx, &mut zip, legacy).await;
    }

    let profile: ModernProfile = serde_json::from_value(profile_value)?;
    let mut version: VersionJson = serde_json::from_slice(&read_entry(&mut zip, &profile.json)?)?;
    if version.inherits_from.is_none() {
        version.inherits_from = Some(game_version.to_owned());
    }

    // Unpack bundled maven artifacts (some are not published remotely).
    let lib_dir = paths.libraries_dir();
    {
        let lib_dir = lib_dir.clone();
        let installer = installer.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut zip = zip::ZipArchive::new(std::fs::File::open(&installer)?)?;
            for i in 0..zip.len() {
                let mut entry = zip.by_index(i)?;
                let Some(rel) = entry.enclosed_name() else {
                    continue;
                };
                let Ok(rel) = rel.strip_prefix("maven") else {
                    continue;
                };
                if entry.is_dir() || rel.as_os_str().is_empty() {
                    continue;
                }
                let out = lib_dir.join(rel);
                if let Some(p) = out.parent() {
                    std::fs::create_dir_all(p)?;
                }
                let mut f = std::fs::File::create(&out)?;
                std::io::copy(&mut entry, &mut f)?;
            }
            Ok(())
        })
        .await
        .map_err(|e| Error::Io(std::io::Error::other(e)))??;
    }

    // Vanilla first: processors need the client jar.
    let vanilla = ctx.meta.resolve(game_version).await?;
    let installer_ctx = Installer {
        paths,
        downloader: ctx.downloader,
        env: ctx.env,
    };
    installer_ctx.install(&vanilla, progress).await?;

    // Libraries for both the processors and the final version.
    let mut tasks = Vec::new();
    for lib in profile.libraries.iter().chain(version.libraries.iter()) {
        for r in lib.resolve(ctx.env) {
            let path = lib_dir.join(&r.path);
            if r.url.is_empty() || path.exists() {
                continue;
            }
            tasks.push(DownloadTask::new(r.url, path).sha1(r.sha1).size(r.size));
        }
    }
    ctx.downloader.download_all(tasks, progress).await?;

    // Resolve data values.
    let work = paths.temp_dir().join(format!("forge-{loader_version}"));
    std::fs::create_dir_all(&work)?;
    let mut data: HashMap<String, String> = HashMap::new();
    for (key, entry) in &profile.data {
        let v = &entry.client;
        let resolved = if let Some(coord) = v.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
            lib_path(&lib_dir, coord)?
        } else if let Some(lit) = v.strip_prefix('\'').and_then(|s| s.strip_suffix('\'')) {
            lit.to_owned()
        } else if v.starts_with('/') {
            let out = work.join(v.trim_start_matches('/'));
            if let Some(p) = out.parent() {
                std::fs::create_dir_all(p)?;
            }
            std::fs::write(&out, read_entry(&mut zip, v)?)?;
            out.to_string_lossy().into_owned()
        } else {
            v.clone()
        };
        data.insert(key.clone(), resolved);
    }
    let path_s = |p: &Path| p.to_string_lossy().into_owned();
    data.insert("SIDE".into(), "client".into());
    data.insert(
        "MINECRAFT_JAR".into(),
        path_s(&paths.version_jar(vanilla.jar_id())),
    );
    data.insert("MINECRAFT_VERSION".into(), game_version.to_owned());
    data.insert("ROOT".into(), path_s(paths.root()));
    data.insert("INSTALLER".into(), path_s(&installer));
    data.insert("LIBRARY_DIR".into(), path_s(&lib_dir));

    let java_req = vanilla.java();
    let java = java::ensure_runtime(
        paths,
        ctx.downloader,
        &java_req.component,
        java_req.major_version,
        &Progress::default(),
    )
    .await?;

    for proc in profile
        .processors
        .iter()
        .filter(|p| p.sides.as_ref().is_none_or(|s| s.iter().any(|s| s == "client")))
    {
        run_processor(proc, &java, &lib_dir, &data).await?;
    }
    let _ = std::fs::remove_dir_all(&work);

    // Saving the version JSON last marks the install as complete.
    ctx.meta.save_version_json(&version).await?;
    Ok(version.id)
}

fn lib_path(lib_dir: &Path, coord: &str) -> Result<String> {
    let c: MavenCoord = coord.parse()?;
    Ok(lib_dir.join(c.path()).to_string_lossy().into_owned())
}

fn resolve_arg(arg: &str, lib_dir: &Path, data: &HashMap<String, String>) -> Result<String> {
    if let Some(key) = arg.strip_prefix('{').and_then(|s| s.strip_suffix('}')) {
        return data
            .get(key)
            .cloned()
            .ok_or_else(|| Error::Loader(format!("installer references unknown value {{{key}}}")));
    }
    if let Some(coord) = arg.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        return lib_path(lib_dir, coord);
    }
    Ok(arg.to_owned())
}

async fn run_processor(
    proc: &Processor,
    java: &Path,
    lib_dir: &Path,
    data: &HashMap<String, String>,
) -> Result<()> {
    // Skip processors whose outputs are already present and correct.
    if !proc.outputs.is_empty() {
        let mut all_ok = true;
        for (file, hash) in &proc.outputs {
            let file = PathBuf::from(resolve_arg(file, lib_dir, data)?);
            let hash = resolve_arg(hash, lib_dir, data)?;
            let hash = hash.trim_matches('\'');
            if !file.exists() || download::sha1_file(&file).await? != hash {
                all_ok = false;
                break;
            }
        }
        if all_ok {
            return Ok(());
        }
    }

    let jar = PathBuf::from(lib_path(lib_dir, &proc.jar)?);
    let main_class = java::jar_main_class(&jar)?;
    let sep = crate::launch::args::CLASSPATH_SEPARATOR;
    let mut cp = vec![jar.to_string_lossy().into_owned()];
    for c in &proc.classpath {
        cp.push(lib_path(lib_dir, c)?);
    }
    let args = proc
        .args
        .iter()
        .map(|a| resolve_arg(a, lib_dir, data))
        .collect::<Result<Vec<_>>>()?;

    tracing::info!(processor = %proc.jar, "running installer processor");
    let mut cmd = tokio::process::Command::new(java);
    cmd.arg("-cp").arg(cp.join(sep)).arg(&main_class).args(&args);
    #[cfg(windows)]
    cmd.creation_flags(0x0800_0000);
    let out = cmd
        .output()
        .await
        .map_err(|e| Error::Loader(format!("could not run installer processor: {e}")))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        let tail: Vec<&str> = stderr.lines().rev().take(15).collect();
        return Err(Error::Loader(format!(
            "installer processor {} failed:\n{}",
            proc.jar,
            tail.into_iter().rev().collect::<Vec<_>>().join("\n")
        )));
    }

    for (file, hash) in &proc.outputs {
        let file = PathBuf::from(resolve_arg(file, lib_dir, data)?);
        let expected = resolve_arg(hash, lib_dir, data)?;
        let expected = expected.trim_matches('\'');
        let actual = download::sha1_file(&file).await?;
        if actual != expected {
            return Err(Error::HashMismatch {
                path: file,
                expected: expected.to_owned(),
                actual,
            });
        }
    }
    Ok(())
}

/// Forge for 1.12.2 and older: the profile embeds the version JSON and the
/// universal jar ships inside the installer.
async fn install_legacy(
    ctx: &ForgeContext<'_>,
    zip: &mut zip::ZipArchive<std::fs::File>,
    legacy: LegacyProfile,
) -> Result<String> {
    let mut version = legacy.version_info;
    if version.inherits_from.is_none() {
        version.inherits_from = Some(legacy.install.minecraft.clone());
    }
    if version.jar.is_none() {
        version.jar = Some(legacy.install.minecraft.clone());
    }
    for lib in &mut version.libraries {
        if let Some(url) = &mut lib.url
            && (url.contains("files.minecraftforge.net/maven") || url.starts_with("http://"))
        {
            *url = url
                .replace("http://files.minecraftforge.net/maven/", FORGE_MAVEN)
                .replace("https://files.minecraftforge.net/maven/", FORGE_MAVEN)
                .replacen("http://", "https://", 1);
        }
    }

    let coord: MavenCoord = legacy.install.path.parse()?;
    let dest = ctx.meta.paths.libraries_dir().join(coord.path());
    if let Some(parent) = dest.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&dest, read_entry(zip, &legacy.install.file_path)?)?;

    ctx.meta.save_version_json(&version).await?;
    Ok(version.id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn neoforge_versions_map_to_game_versions() {
        assert_eq!(neoforge_game_version("21.1.77").as_deref(), Some("1.21.1"));
        assert_eq!(neoforge_game_version("20.4.237").as_deref(), Some("1.20.4"));
        assert_eq!(neoforge_game_version("21.0.1-beta").as_deref(), Some("1.21"));
        assert_eq!(neoforge_game_version("26.1.0.1").as_deref(), Some("26.1"));
        assert_eq!(neoforge_game_version("nope"), None);
    }

    #[test]
    fn installer_urls() {
        assert_eq!(
            Flavor::Forge.installer_url("1.20.1", "47.2.0"),
            "https://maven.minecraftforge.net/net/minecraftforge/forge/1.20.1-47.2.0/forge-1.20.1-47.2.0-installer.jar"
        );
        assert_eq!(
            Flavor::NeoForge.installer_url("1.21.1", "21.1.77"),
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.77/neoforge-21.1.77-installer.jar"
        );
    }

    #[test]
    fn resolves_processor_args() {
        let data = HashMap::from([("MAPPINGS".to_string(), "/tmp/m.txt".to_string())]);
        let lib = Path::new("/libs");
        assert_eq!(resolve_arg("{MAPPINGS}", lib, &data).unwrap(), "/tmp/m.txt");
        assert_eq!(
            resolve_arg("[net.minecraft:client:1.20.1:srg]", lib, &data).unwrap(),
            Path::new("/libs")
                .join("net/minecraft/client/1.20.1/client-1.20.1-srg.jar")
                .to_string_lossy()
        );
        assert_eq!(resolve_arg("--side", lib, &data).unwrap(), "--side");
        assert!(resolve_arg("{MISSING}", lib, &data).is_err());
    }
}
