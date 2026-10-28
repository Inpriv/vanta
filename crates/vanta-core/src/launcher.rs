//! High-level orchestration: from an instance to a running game.

use std::path::PathBuf;

use crate::download::{Downloader, Progress};
use crate::error::{Error, Result};
use crate::install::{self, InstalledVersion, Installer};
use crate::instance::{Instance, InstanceStore, LoaderKind};
use crate::java;
use crate::launch::{self, Command, GameProcess, LaunchAuth, LaunchOptions};
use crate::loaders;
use crate::meta::Meta;
use crate::paths::Paths;
use crate::rules::Environment;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stage {
    Resolving,
    InstallingLoader,
    Downloading,
    PreparingJava,
    /// First launch of a version needing a Java the user does not have yet.
    DownloadingJava(u32),
    Starting,
}

impl Stage {
    pub fn label(self) -> String {
        match self {
            Stage::Resolving => "Preparing".into(),
            Stage::InstallingLoader => "Installing mod loader".into(),
            Stage::Downloading => "Downloading files".into(),
            Stage::PreparingJava => "Checking Java".into(),
            Stage::DownloadingJava(major) => format!("Downloading Java {major}"),
            Stage::Starting => "Starting game".into(),
        }
    }
}

/// Launcher-wide defaults that instances may override.
#[derive(Clone, Debug)]
pub struct GlobalLaunchSettings {
    pub memory_mb: u32,
    pub java_path: Option<PathBuf>,
    pub jvm_args: String,
}

#[derive(Clone)]
pub struct Launcher {
    pub paths: Paths,
    pub client: reqwest::Client,
    pub downloader: Downloader,
    pub meta: Meta,
    pub env: Environment,
    pub instances: InstanceStore,
}

impl Launcher {
    pub fn new(paths: Paths) -> Self {
        let client = crate::http::client();
        Self {
            downloader: Downloader::new(client.clone()),
            meta: Meta::new(paths.clone(), client.clone()),
            env: Environment::current(),
            instances: InstanceStore::new(paths.instances_dir()),
            client,
            paths,
        }
    }

    /// Make sure the instance's version (and loader) is installed; returns the
    /// version id to launch.
    pub async fn ensure_version(
        &self,
        inst: &mut Instance,
        progress: &Progress,
        on_stage: &(dyn Fn(Stage) + Send + Sync),
    ) -> Result<String> {
        if inst.loader == LoaderKind::Vanilla {
            return Ok(inst.game_version.clone());
        }
        if let Some(id) = &inst.installed_version_id
            && self.paths.version_json(id).exists()
        {
            return Ok(id.clone());
        }
        on_stage(Stage::InstallingLoader);
        let loader_version = match &inst.loader_version {
            Some(v) => v.clone(),
            None => {
                let versions = loaders::versions(&self.meta, inst.loader, &inst.game_version).await?;
                let pick = versions
                    .iter()
                    .find(|v| v.stable)
                    .or(versions.first())
                    .ok_or_else(|| {
                        Error::Loader(format!(
                            "{} is not available for Minecraft {}",
                            inst.loader.display_name(),
                            inst.game_version
                        ))
                    })?;
                pick.version.clone()
            }
        };
        let id = loaders::install(
            &self.meta,
            &self.downloader,
            &self.env,
            inst.loader,
            &inst.game_version,
            &loader_version,
            progress,
        )
        .await?;
        inst.loader_version = Some(loader_version);
        inst.installed_version_id = Some(id.clone());
        self.instances.save(inst)?;
        Ok(id)
    }

    pub async fn install(
        &self,
        inst: &mut Instance,
        progress: &Progress,
        on_stage: &(dyn Fn(Stage) + Send + Sync),
    ) -> Result<InstalledVersion> {
        on_stage(Stage::Resolving);
        let id = self.ensure_version(inst, progress, on_stage).await?;
        let version = self.meta.resolve(&id).await?;
        on_stage(Stage::Downloading);
        progress.reset();
        let installer = Installer {
            paths: &self.paths,
            downloader: &self.downloader,
            env: &self.env,
        };
        installer.install(&version, progress).await
    }

    pub async fn java_for(
        &self,
        inst: &Instance,
        installed: &InstalledVersion,
        global: &GlobalLaunchSettings,
        progress: &Progress,
        on_stage: &(dyn Fn(Stage) + Send + Sync),
    ) -> Result<PathBuf> {
        if let Some(p) = inst.java_path.clone().or_else(|| global.java_path.clone()) {
            if p.is_file() {
                return Ok(p);
            }
            return Err(Error::Java(format!(
                "configured Java not found at {}",
                p.display()
            )));
        }
        let req = installed.version.java();
        if let Some(java) = java::installed_runtime(&self.paths, &req.component, req.major_version) {
            return Ok(java);
        }
        // Nothing suitable yet: download Mojang's runtime (or Adoptium's) automatically.
        on_stage(Stage::DownloadingJava(req.major_version));
        java::ensure_runtime(
            &self.paths,
            &self.downloader,
            &req.component,
            req.major_version,
            progress,
        )
        .await
    }

    /// Install everything needed and start the game.
    pub async fn launch(
        &self,
        instance_id: &str,
        auth: LaunchAuth,
        global: &GlobalLaunchSettings,
        progress: &Progress,
        on_stage: &(dyn Fn(Stage) + Send + Sync),
    ) -> Result<(GameProcess, Command)> {
        let mut inst = self.instances.get(instance_id)?;
        let installed = self.install(&mut inst, progress, on_stage).await?;

        on_stage(Stage::PreparingJava);
        progress.reset();
        let java = self
            .java_for(&inst, &installed, global, progress, on_stage)
            .await?;

        on_stage(Stage::Starting);
        let game_dir = self.instances.game_dir(&inst.id);
        if installed.map_to_resources {
            let (installed, game_dir) = (installed.clone(), game_dir.clone());
            tokio::task::spawn_blocking(move || install::map_resources(&installed, &game_dir))
                .await
                .map_err(|e| Error::Io(std::io::Error::other(e)))??;
        }

        let mut env = self.env.clone();
        env.features.has_custom_resolution = inst.resolution.is_some();
        let mut extra_jvm = crate::instance::split_args(&global.jvm_args);
        extra_jvm.extend(inst.jvm_args_list());
        let opts = LaunchOptions {
            java,
            game_dir: game_dir.clone(),
            assets_dir: self.paths.assets_dir(),
            libraries_dir: self.paths.libraries_dir(),
            auth,
            min_memory_mb: None,
            max_memory_mb: inst.memory_mb.unwrap_or(global.memory_mb),
            extra_jvm_args: extra_jvm,
            resolution: inst.resolution.map(|r| (r.width, r.height)),
            launcher_name: "vanta".into(),
            launcher_version: env!("CARGO_PKG_VERSION").into(),
            client_id: String::new(),
        };
        let command = launch::build(&installed, &opts, &env);
        tracing::info!(
            instance = %inst.id,
            version = %installed.version.id,
            args = ?command.redacted(&opts.auth.access_token),
            "launching"
        );
        let process = launch::spawn(&command, &game_dir)?;

        inst.last_played = Some(chrono::Utc::now());
        self.instances.save(&inst)?;
        Ok((process, command))
    }
}
