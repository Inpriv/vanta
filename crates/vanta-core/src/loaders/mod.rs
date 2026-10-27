//! Mod loader installation.

pub mod fabric;
pub mod forge;

use crate::download::{Downloader, Progress};
use crate::error::Result;
use crate::instance::LoaderKind;
use crate::meta::Meta;
use crate::rules::Environment;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LoaderVersion {
    pub version: String,
    /// Recommended / stable build.
    pub stable: bool,
}

/// Available loader versions for a game version, newest first.
pub async fn versions(meta: &Meta, kind: LoaderKind, game_version: &str) -> Result<Vec<LoaderVersion>> {
    match kind {
        LoaderKind::Vanilla => Ok(Vec::new()),
        LoaderKind::Fabric => fabric::versions(meta, fabric::FABRIC_META, game_version).await,
        LoaderKind::Quilt => fabric::versions(meta, fabric::QUILT_META, game_version).await,
        LoaderKind::Forge => forge::versions(meta, forge::Flavor::Forge, game_version).await,
        LoaderKind::NeoForge => forge::versions(meta, forge::Flavor::NeoForge, game_version).await,
    }
}

/// Install a loader and return the version id to launch.
pub async fn install(
    meta: &Meta,
    downloader: &Downloader,
    env: &Environment,
    kind: LoaderKind,
    game_version: &str,
    loader_version: &str,
    progress: &Progress,
) -> Result<String> {
    let ctx = forge::ForgeContext {
        meta,
        downloader,
        env,
    };
    match kind {
        LoaderKind::Vanilla => Ok(game_version.to_owned()),
        LoaderKind::Fabric => fabric::install(meta, fabric::FABRIC_META, game_version, loader_version).await,
        LoaderKind::Quilt => fabric::install(meta, fabric::QUILT_META, game_version, loader_version).await,
        LoaderKind::Forge => {
            forge::install(&ctx, forge::Flavor::Forge, game_version, loader_version, progress).await
        }
        LoaderKind::NeoForge => {
            forge::install(
                &ctx,
                forge::Flavor::NeoForge,
                game_version,
                loader_version,
                progress,
            )
            .await
        }
    }
}
