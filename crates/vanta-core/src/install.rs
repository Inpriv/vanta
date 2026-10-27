//! Installing a resolved version: client jar, libraries, natives, assets and
//! logging configuration.

use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::download::{DownloadTask, Downloader, Progress};
use crate::error::{Error, Result};
use crate::paths::{self, Paths};
use crate::rules::Environment;
use crate::version::{ResolvedLibrary, VersionJson};

pub const RESOURCES_URL: &str = "https://resources.download.minecraft.net/";

#[derive(Debug, Deserialize)]
pub struct AssetObject {
    pub hash: String,
    pub size: u64,
}

#[derive(Debug, Deserialize)]
pub struct AssetIndex {
    pub objects: HashMap<String, AssetObject>,
    #[serde(default, rename = "virtual")]
    pub is_virtual: bool,
    #[serde(default)]
    pub map_to_resources: bool,
}

/// Everything needed to build the launch command after installation.
#[derive(Clone, Debug)]
pub struct InstalledVersion {
    pub version: VersionJson,
    pub client_jar: PathBuf,
    /// Classpath libraries in order (absolute paths).
    pub classpath: Vec<PathBuf>,
    pub natives_dir: PathBuf,
    /// Value for `${game_assets}` (virtual/legacy assets directory).
    pub game_assets: PathBuf,
    /// Asset index requires copying into `<game dir>/resources`.
    pub map_to_resources: bool,
    pub logging_config: Option<PathBuf>,
}

pub struct Installer<'a> {
    pub paths: &'a Paths,
    pub downloader: &'a Downloader,
    pub env: &'a Environment,
}

impl Installer<'_> {
    pub fn libraries(&self, version: &VersionJson) -> Vec<ResolvedLibrary> {
        version
            .libraries
            .iter()
            .flat_map(|l| l.resolve(self.env))
            .collect()
    }

    pub async fn install(&self, version: &VersionJson, progress: &Progress) -> Result<InstalledVersion> {
        let libraries = self.libraries(version);
        let lib_dir = self.paths.libraries_dir();
        let mut tasks = Vec::new();

        // Client jar. Loader versions get their own copy named after the
        // loader version id, because some loaders (Forge) look the game jar up
        // by `${version_name}.jar`.
        let jar_id = version.jar_id().to_owned();
        let vanilla_jar = self.paths.version_jar(&jar_id);
        if let Some(client) = version.downloads.as_ref().and_then(|d| d.client.as_ref()) {
            tasks.push(
                DownloadTask::new(&client.url, &vanilla_jar)
                    .sha1(client.sha1.clone())
                    .size(client.size),
            );
        }

        for lib in &libraries {
            let path = lib_dir.join(&lib.path);
            if lib.url.is_empty() {
                // Produced by a loader installer; must already exist.
                if !path.exists() {
                    return Err(Error::Loader(format!(
                        "missing library {} (reinstall the mod loader)",
                        lib.path
                    )));
                }
                continue;
            }
            tasks.push(
                DownloadTask::new(&lib.url, path)
                    .sha1(lib.sha1.clone())
                    .size(lib.size),
            );
        }

        let logging_config = match version.logging.as_ref().and_then(|l| l.client.as_ref()) {
            Some(cfg) => {
                let path = self.paths.log_configs_dir().join(&cfg.file.id);
                tasks.push(
                    DownloadTask::new(&cfg.file.url, &path)
                        .sha1(Some(cfg.file.sha1.clone()))
                        .size(Some(cfg.file.size)),
                );
                Some(path)
            }
            None => None,
        };

        // Asset index is small; fetch it first so its objects join the batch.
        let mut asset_index = None;
        if let Some(index_ref) = &version.asset_index {
            let path = self.paths.asset_index(&index_ref.id);
            let task = DownloadTask::new(&index_ref.url, &path)
                .sha1(Some(index_ref.sha1.clone()))
                .size(Some(index_ref.size));
            self.downloader.fetch(&task, &Progress::default()).await?;
            let index: AssetIndex = serde_json::from_slice(&tokio::fs::read(&path).await?)?;
            for obj in index.objects.values() {
                tasks.push(
                    DownloadTask::new(
                        format!("{RESOURCES_URL}{}/{}", &obj.hash[..2], obj.hash),
                        self.paths.asset_object(&obj.hash),
                    )
                    .sha1(Some(obj.hash.clone()))
                    .size(Some(obj.size)),
                );
            }
            asset_index = Some(index);
        }

        self.downloader.download_all(tasks, progress).await?;

        let client_jar = if jar_id == version.id {
            vanilla_jar
        } else {
            let own = self.paths.version_jar(&version.id);
            if !own.exists() && vanilla_jar.exists() {
                paths::link_or_copy(&vanilla_jar, &own)?;
            }
            own
        };

        let natives_dir = self.paths.natives_dir(&version.id);
        let natives: Vec<_> = libraries
            .iter()
            .filter_map(|l| l.extract.clone().map(|ex| (lib_dir.join(&l.path), ex)))
            .collect();
        let game_assets = self.paths.virtual_assets(version.asset_index_id());
        let index_meta = asset_index.as_ref().map(|i| (i.is_virtual, i.map_to_resources));
        {
            let natives_dir = natives_dir.clone();
            let game_assets = game_assets.clone();
            let paths = self.paths.clone();
            let objects: Vec<(String, String)> = match &asset_index {
                Some(i) if i.is_virtual || i.map_to_resources => i
                    .objects
                    .iter()
                    .map(|(k, v)| (k.clone(), v.hash.clone()))
                    .collect(),
                _ => Vec::new(),
            };
            tokio::task::spawn_blocking(move || -> Result<()> {
                extract_natives(&natives, &natives_dir)?;
                for (name, hash) in objects {
                    let dst = game_assets.join(&name);
                    if !dst.exists() {
                        paths::link_or_copy(&paths.asset_object(&hash), &dst)?;
                    }
                }
                Ok(())
            })
            .await
            .map_err(|e| Error::Io(std::io::Error::other(e)))??;
        }

        let mut classpath: Vec<PathBuf> = libraries
            .iter()
            .filter(|l| l.extract.is_none())
            .map(|l| lib_dir.join(&l.path))
            .collect();
        classpath.push(client_jar.clone());

        Ok(InstalledVersion {
            version: version.clone(),
            client_jar,
            classpath,
            natives_dir,
            game_assets,
            map_to_resources: index_meta.is_some_and(|(_, m)| m),
            logging_config,
        })
    }
}

/// Extract native libraries from their jars, skipping excluded prefixes.
pub fn extract_natives(natives: &[(PathBuf, Vec<String>)], dest: &Path) -> Result<()> {
    std::fs::create_dir_all(dest).map_err(Error::io_at(dest))?;
    for (jar, exclude) in natives {
        let file = std::fs::File::open(jar).map_err(Error::io_at(jar))?;
        let mut archive = zip::ZipArchive::new(file)?;
        for i in 0..archive.len() {
            let mut entry = archive.by_index(i)?;
            let name = entry.name().to_owned();
            if entry.is_dir()
                || name.starts_with("META-INF/")
                || exclude.iter().any(|e| name.starts_with(e.as_str()))
            {
                continue;
            }
            let Some(rel) = entry.enclosed_name() else {
                continue;
            };
            let out = dest.join(rel);
            if let Ok(meta) = out.metadata()
                && meta.len() == entry.size()
            {
                continue;
            }
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut buf = Vec::with_capacity(entry.size() as usize);
            entry.read_to_end(&mut buf)?;
            std::fs::write(&out, buf).map_err(Error::io_at(&out))?;
        }
    }
    Ok(())
}

/// Copy assets into `<game dir>/resources` for very old versions.
pub fn map_resources(installed: &InstalledVersion, game_dir: &Path) -> Result<()> {
    let resources = game_dir.join("resources");
    copy_tree(&installed.game_assets, &resources)
}

fn copy_tree(src: &Path, dst: &Path) -> Result<()> {
    if !src.exists() {
        return Ok(());
    }
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let target = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_tree(&entry.path(), &target)?;
        } else if !target.exists() {
            paths::link_or_copy(&entry.path(), &target)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn extracts_natives_with_excludes() {
        let dir = tempfile::tempdir().unwrap();
        let jar = dir.path().join("n.jar");
        {
            let mut zip = zip::ZipWriter::new(std::fs::File::create(&jar).unwrap());
            let opts = zip::write::SimpleFileOptions::default();
            zip.start_file("liblwjgl.so", opts).unwrap();
            zip.write_all(b"elf").unwrap();
            zip.start_file("META-INF/MANIFEST.MF", opts).unwrap();
            zip.write_all(b"m").unwrap();
            zip.start_file("skip/me.txt", opts).unwrap();
            zip.write_all(b"x").unwrap();
            zip.finish().unwrap();
        }
        let out = dir.path().join("natives");
        extract_natives(&[(jar, vec!["skip/".into()])], &out).unwrap();
        assert_eq!(std::fs::read(out.join("liblwjgl.so")).unwrap(), b"elf");
        assert!(!out.join("META-INF").exists());
        assert!(!out.join("skip").exists());
    }
}
