//! Fabric and Quilt share the same "meta" API shape: a loader list per game
//! version and a ready-made version JSON profile.

use serde::Deserialize;

use super::LoaderVersion;
use crate::download;
use crate::error::{Error, Result};
use crate::meta::Meta;
use crate::version::VersionJson;

pub const FABRIC_META: &str = "https://meta.fabricmc.net/v2";
pub const QUILT_META: &str = "https://meta.quiltmc.org/v3";

#[derive(Deserialize)]
struct Entry {
    loader: LoaderInfo,
}

#[derive(Deserialize)]
struct LoaderInfo {
    version: String,
    #[serde(default)]
    stable: Option<bool>,
}

pub async fn versions(meta: &Meta, base: &str, game_version: &str) -> Result<Vec<LoaderVersion>> {
    let url = format!("{base}/versions/loader/{game_version}");
    let entries: Vec<Entry> = download::fetch_json(&meta.client, &url).await?;
    Ok(entries
        .into_iter()
        .map(|e| {
            // Quilt has no `stable` flag; pre-releases carry a suffix instead.
            let stable = e.loader.stable.unwrap_or(!e.loader.version.contains('-'));
            LoaderVersion {
                version: e.loader.version,
                stable,
            }
        })
        .collect())
}

pub async fn install(meta: &Meta, base: &str, game_version: &str, loader_version: &str) -> Result<String> {
    let url = format!("{base}/versions/loader/{game_version}/{loader_version}/profile/json");
    let profile: VersionJson = download::fetch_json(&meta.client, &url)
        .await
        .map_err(|e| Error::Loader(format!("could not fetch loader profile: {e}")))?;
    meta.save_version_json(&profile).await?;
    Ok(profile.id)
}
