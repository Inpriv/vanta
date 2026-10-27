//! Loading version metadata: the Mojang manifest, version JSON files and
//! `inheritsFrom` chains.

use std::future::Future;
use std::pin::Pin;

use crate::download::{self, DownloadTask, Progress};
use crate::error::{Error, Result};
use crate::paths::Paths;
use crate::version::{MANIFEST_URL, VersionJson, VersionManifest};

#[derive(Clone)]
pub struct Meta {
    pub paths: Paths,
    pub client: reqwest::Client,
    downloader: download::Downloader,
}

impl Meta {
    pub fn new(paths: Paths, client: reqwest::Client) -> Self {
        let downloader = download::Downloader::new(client.clone());
        Self {
            paths,
            client,
            downloader,
        }
    }

    pub async fn manifest(&self) -> Result<VersionManifest> {
        download::fetch_json_cached(&self.client, MANIFEST_URL, &self.paths.manifest_cache()).await
    }

    /// The raw JSON of one version (downloaded on demand for vanilla ids).
    pub async fn version_json(&self, id: &str) -> Result<VersionJson> {
        let path = self.paths.version_json(id);
        if let Ok(bytes) = tokio::fs::read(&path).await
            && let Ok(v) = serde_json::from_slice::<VersionJson>(&bytes)
        {
            return Ok(v);
        }
        let manifest = self.manifest().await?;
        let entry = manifest
            .get(id)
            .ok_or_else(|| Error::VersionNotFound(id.to_owned()))?;
        let task = DownloadTask::new(&entry.url, &path).sha1(entry.sha1.clone());
        self.downloader.fetch(&task, &Progress::default()).await?;
        Ok(serde_json::from_slice(&tokio::fs::read(&path).await?)?)
    }

    /// Store a version JSON produced by a mod loader.
    pub async fn save_version_json(&self, version: &VersionJson) -> Result<()> {
        let path = self.paths.version_json(&version.id);
        if let Some(parent) = path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        tokio::fs::write(&path, serde_json::to_vec_pretty(version)?).await?;
        Ok(())
    }

    /// A version with its whole `inheritsFrom` chain merged in.
    pub fn resolve<'a>(
        &'a self,
        id: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<VersionJson>> + Send + 'a>> {
        Box::pin(async move {
            let version = self.version_json(id).await?;
            match version.inherits_from.clone() {
                Some(parent_id) if parent_id != version.id => {
                    let parent = self.resolve(&parent_id).await?;
                    Ok(version.merge_onto(parent))
                }
                _ => Ok(version),
            }
        })
    }
}
