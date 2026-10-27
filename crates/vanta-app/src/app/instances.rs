//! Library: listing, creating and editing instances; version pickers;
//! modpack import/export.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use chrono::Utc;
use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};
use vanta_core::download::Progress;
use vanta_core::instance::{Instance, LoaderKind, Resolution};
use vanta_core::version::VersionType;

use super::Ctrl;
use crate::format;
use crate::{App, AppWindow, InstanceItem, Page, VersionItem};

static LOADER_REQUEST: AtomicU64 = AtomicU64::new(0);

pub fn to_item(inst: &Instance, running: bool) -> InstanceItem {
    let res = inst.resolution.unwrap_or(Resolution {
        width: 1280,
        height: 720,
    });
    InstanceItem {
        id: inst.id.clone().into(),
        name: inst.name.clone().into(),
        initial: format::initial(&inst.name).into(),
        icon: inst.icon.clone().into(),
        game_version: inst.game_version.clone().into(),
        loader: inst.loader.display_name().into(),
        loader_index: inst.loader.index() as i32,
        loader_version: inst.loader_version.clone().unwrap_or_default().into(),
        last_played: inst
            .last_played
            .map(|t| format::relative_time(t, Utc::now()))
            .unwrap_or_default()
            .into(),
        running,
        memory_mb: inst.memory_mb.unwrap_or(0) as i32,
        jvm_args: inst.jvm_args.clone().into(),
        java_path: inst
            .java_path
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_default()
            .into(),
        custom_resolution: inst.resolution.is_some(),
        width: res.width as i32,
        height: res.height as i32,
    }
}

/// Re-read all instances from disk and refresh every view that shows them.
pub fn reload(ctrl: &Ctrl) {
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let store = ctrl.shared.launcher.instances.clone();
        let list = tokio::task::spawn_blocking(move || store.list())
            .await
            .unwrap_or_default();
        let running: Vec<String> = ctrl.shared.running.lock().unwrap().keys().cloned().collect();
        let items: Vec<InstanceItem> = list.iter().map(|i| to_item(i, running.contains(&i.id))).collect();
        let featured_pref = ctrl.settings().featured_instance;
        tracing::debug!(count = items.len(), "instances loaded");
        ctrl.with_ui(move |ui| apply(&ui, items, featured_pref));
    });
}

fn apply(ui: &AppWindow, items: Vec<InstanceItem>, featured_pref: Option<String>) {
    let app = ui.global::<App>();
    let find = |id: &str| items.iter().find(|i| i.id == id).cloned();

    // Featured: whatever is running, else the user's pick, else most recent.
    let featured = items
        .iter()
        .find(|i| i.running)
        .cloned()
        .or_else(|| featured_pref.as_deref().and_then(find))
        .or_else(|| items.first().cloned());
    app.set_has_featured(featured.is_some());
    if let Some(f) = featured {
        app.set_featured_id(f.id.clone());
        app.set_featured(f);
    }

    let selected = app.get_selected_id();
    if let Some(s) = find(&selected) {
        app.set_selected(s);
    } else if app.get_page() == Page::Instance {
        app.set_page(Page::Instances);
    }

    let target = app.get_browse_target_id();
    let target = if find(&target).is_some() {
        target
    } else {
        app.get_featured_id()
    };
    app.set_browse_target_id(target.clone());
    app.set_browse_target_index(
        items
            .iter()
            .position(|i| i.id == target)
            .map(|p| p as i32)
            .unwrap_or(-1),
    );
    let names: Vec<SharedString> = items.iter().map(|i| i.name.clone()).collect();
    app.set_instance_names(ModelRc::new(VecModel::from(names)));
    app.set_instances(ModelRc::new(VecModel::from(items)));
    app.set_instances_loaded(true);
}

fn modify(ctrl: &Ctrl, id: String, f: impl FnOnce(&mut Instance) + Send + 'static) {
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let store = ctrl.shared.launcher.instances.clone();
        let res = tokio::task::spawn_blocking(move || {
            let mut inst = store.get(&id)?;
            f(&mut inst);
            store.save(&inst)
        })
        .await;
        match res {
            Ok(Ok(())) => reload(&ctrl),
            Ok(Err(e)) => ctrl.error(format!("Could not save the instance: {e}")),
            Err(e) => ctrl.error(e),
        }
    });
}

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();

    app.on_select_instance({
        let ctrl = ctrl.clone();
        move |id| {
            let Some(ui) = ctrl.ui.upgrade() else { return };
            let app = ui.global::<App>();
            app.set_selected_id(id.clone());
            if let Some(item) = (0..app.get_instances().row_count())
                .filter_map(|i| app.get_instances().row_data(i))
                .find(|i| i.id == id)
            {
                app.set_selected(item);
            }
            app.set_content_kind(0);
            app.invoke_refresh_content();
        }
    });

    app.on_feature_instance({
        let ctrl = ctrl.clone();
        move |id| {
            let settings = {
                let mut s = ctrl.shared.settings.lock().unwrap();
                s.featured_instance = Some(id.to_string());
                s.clone()
            };
            let _ = settings.save();
            reload(&ctrl);
        }
    });

    app.on_create_instance({
        let ctrl = ctrl.clone();
        move |name, version, loader, loader_version, icon| {
            let mut inst = Instance::new(name.trim(), version.as_str());
            inst.loader = LoaderKind::from_index(loader as usize);
            inst.loader_version = (!loader_version.is_empty()).then(|| loader_version.to_string());
            inst.icon = icon.to_string();
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let store = ctrl.shared.launcher.instances.clone();
                match tokio::task::spawn_blocking(move || store.create(inst)).await {
                    Ok(Ok(inst)) => {
                        {
                            let mut s = ctrl.shared.settings.lock().unwrap();
                            s.featured_instance = Some(inst.id.clone());
                            let _ = s.save();
                        }
                        ctrl.toast(format!("Created {}", inst.name));
                        reload(&ctrl);
                    }
                    Ok(Err(e)) => ctrl.error(format!("Could not create the instance: {e}")),
                    Err(e) => ctrl.error(e),
                }
            });
        }
    });

    app.on_rename_instance({
        let ctrl = ctrl.clone();
        move |id, name| {
            let name = name.trim().to_owned();
            if !name.is_empty() {
                modify(&ctrl, id.to_string(), move |i| i.name = name);
            }
        }
    });

    app.on_set_instance_icon({
        let ctrl = ctrl.clone();
        move |id, icon| {
            let icon = icon.to_string();
            modify(&ctrl, id.to_string(), move |i| i.icon = icon);
        }
    });

    app.on_save_instance({
        let ctrl = ctrl.clone();
        move |item| {
            modify(&ctrl, item.id.to_string(), move |i| {
                i.memory_mb = (item.memory_mb > 0).then_some(item.memory_mb as u32);
                i.jvm_args = item.jvm_args.trim().to_owned();
                let java = item.java_path.trim();
                i.java_path = (!java.is_empty()).then(|| PathBuf::from(java));
                i.resolution = item.custom_resolution.then(|| Resolution {
                    width: (item.width.max(320)) as u32,
                    height: (item.height.max(240)) as u32,
                });
            });
            ctrl.toast("Saved");
        }
    });

    app.on_browse_instance_java({
        let ctrl = ctrl.clone();
        move || {
            let Some(ui) = ctrl.ui.upgrade() else { return };
            let id = ui.global::<App>().get_selected_id().to_string();
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                if let Some(file) = rfd::AsyncFileDialog::new()
                    .set_title("Choose a Java executable")
                    .pick_file()
                    .await
                {
                    let path = file.path().to_path_buf();
                    modify(&ctrl, id, move |i| i.java_path = Some(path));
                }
            });
        }
    });

    app.on_duplicate_instance({
        let ctrl = ctrl.clone();
        move |id| {
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let store = ctrl.shared.launcher.instances.clone();
                let res = tokio::task::spawn_blocking(move || {
                    let name = format!("{} copy", store.get(&id)?.name);
                    store.duplicate(&id, &name)
                })
                .await;
                match res {
                    Ok(Ok(inst)) => {
                        ctrl.toast(format!("Created {}", inst.name));
                        reload(&ctrl);
                    }
                    Ok(Err(e)) => ctrl.error(format!("Could not duplicate: {e}")),
                    Err(e) => ctrl.error(e),
                }
            });
        }
    });

    app.on_delete_instance({
        let ctrl = ctrl.clone();
        move |id| {
            if ctrl.shared.running.lock().unwrap().contains_key(id.as_str()) {
                ctrl.error("Stop the game before deleting this instance.");
                return;
            }
            let ctrl = ctrl.clone();
            ctrl.with_ui(|ui| ui.global::<App>().set_page(Page::Instances));
            ctrl.clone().spawn(async move {
                let store = ctrl.shared.launcher.instances.clone();
                let id2 = id.to_string();
                match tokio::task::spawn_blocking(move || store.delete(&id2)).await {
                    Ok(Ok(())) => {
                        ctrl.toast("Instance deleted");
                        reload(&ctrl);
                    }
                    Ok(Err(e)) => ctrl.error(format!("Could not delete: {e}")),
                    Err(e) => ctrl.error(e),
                }
            });
        }
    });

    app.on_open_instance_folder({
        let ctrl = ctrl.clone();
        move |id| {
            let dir = ctrl.shared.launcher.instances.game_dir(&id);
            let _ = std::fs::create_dir_all(&dir);
            let _ = open::that_detached(dir);
        }
    });

    app.on_refresh_versions({
        let ctrl = ctrl.clone();
        move |filter| refresh_versions(&ctrl, filter.to_string())
    });

    app.on_request_loader_versions({
        let ctrl = ctrl.clone();
        move |game, loader| {
            let kind = LoaderKind::from_index(loader as usize);
            let serial = LOADER_REQUEST.fetch_add(1, Ordering::Relaxed) + 1;
            ctrl.with_ui(|ui| {
                let app = ui.global::<App>();
                app.set_loader_versions(ModelRc::default());
                app.set_loader_versions_loading(true);
            });
            if kind == LoaderKind::Vanilla {
                ctrl.with_ui(|ui| ui.global::<App>().set_loader_versions_loading(false));
                return;
            }
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let res = vanta_core::loaders::versions(&ctrl.shared.launcher.meta, kind, &game).await;
                if LOADER_REQUEST.load(Ordering::Relaxed) != serial {
                    return;
                }
                let versions = match res {
                    Ok(mut v) => {
                        // Recommended build first, the rest newest-first.
                        if let Some(pos) = v.iter().position(|x| x.stable) {
                            let s = v.remove(pos);
                            v.insert(0, s);
                        }
                        v.into_iter()
                            .map(|x| SharedString::from(x.version))
                            .collect::<Vec<_>>()
                    }
                    Err(e) => {
                        ctrl.error(format!("Could not load {} versions: {e}", kind.display_name()));
                        Vec::new()
                    }
                };
                ctrl.with_ui(move |ui| {
                    let app = ui.global::<App>();
                    app.set_loader_versions(ModelRc::new(VecModel::from(versions)));
                    app.set_loader_versions_loading(false);
                });
            });
        }
    });

    app.on_import_mrpack({
        let ctrl = ctrl.clone();
        move || {
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let Some(file) = rfd::AsyncFileDialog::new()
                    .set_title("Import a Modrinth modpack")
                    .add_filter("Modrinth modpack", &["mrpack"])
                    .pick_file()
                    .await
                else {
                    return;
                };
                import_pack(&ctrl, file.path().to_path_buf()).await;
            });
        }
    });

    app.on_export_mrpack({
        let ctrl = ctrl.clone();
        move |id, version, name, mods, config, packs, options| {
            let mut include = Vec::new();
            if mods {
                include.push("mods".to_owned());
            }
            if config {
                include.push("config".to_owned());
            }
            if packs {
                include.extend(["resourcepacks".to_owned(), "shaderpacks".to_owned()]);
            }
            if options {
                include.push("options.txt".to_owned());
            }
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                if let Err(e) = export_pack(
                    &ctrl,
                    id.to_string(),
                    version.to_string(),
                    name.to_string(),
                    include,
                )
                .await
                {
                    ctrl.error(format!("Export failed: {e}"));
                }
            });
        }
    });
}

pub async fn import_pack(ctrl: &Ctrl, path: PathBuf) {
    ctrl.toast("Importing modpack…");
    let l = &ctrl.shared.launcher;
    let progress = Progress::new();
    match vanta_core::mrpack::import(&l.paths, &l.instances, &l.downloader, &path, &progress).await {
        Ok(inst) => {
            {
                let mut s = ctrl.shared.settings.lock().unwrap();
                s.featured_instance = Some(inst.id.clone());
                let _ = s.save();
            }
            ctrl.toast(format!("Imported {}", inst.name));
            reload(ctrl);
        }
        Err(e) => ctrl.error(format!("Import failed: {e}")),
    }
}

async fn export_pack(
    ctrl: &Ctrl,
    id: String,
    version: String,
    name: String,
    include: Vec<String>,
) -> anyhow::Result<()> {
    let Some(out) = rfd::AsyncFileDialog::new()
        .set_title("Export modpack")
        .set_file_name(format!(
            "{}-{}.mrpack",
            vanta_core::instance::slugify(&name),
            version
        ))
        .add_filter("Modrinth modpack", &["mrpack"])
        .save_file()
        .await
    else {
        return Ok(());
    };
    let out = out.path().to_path_buf();
    let store = ctrl.shared.launcher.instances.clone();
    let inst = store.get(&id)?;
    let game_dir = store.game_dir(&id);

    // Hash content files and ask Modrinth which ones it hosts.
    let dirs = ["mods", "resourcepacks", "shaderpacks"];
    let gd = game_dir.clone();
    let hashes: Vec<String> = tokio::task::spawn_blocking(move || {
        dirs.iter()
            .filter_map(|d| std::fs::read_dir(gd.join(d)).ok())
            .flatten()
            .flatten()
            .filter(|e| e.path().is_file())
            .filter_map(|e| vanta_core::download::sha1_file_blocking(&e.path()).ok())
            .collect()
    })
    .await?;
    let mut known = HashMap::new();
    for chunk in hashes.chunks(500) {
        if let Ok(found) = ctrl.shared.modrinth.versions_by_hashes(chunk).await {
            for (sha1, v) in found {
                if let Some(f) = v.files.iter().find(|f| f.hashes.sha1 == sha1) {
                    known.insert(sha1, f.url.clone());
                }
            }
        }
    }

    let opts = vanta_core::mrpack::ExportOptions {
        name,
        version,
        summary: None,
        include,
    };
    let out2 = out.clone();
    tokio::task::spawn_blocking(move || vanta_core::mrpack::export(&inst, &game_dir, &opts, &known, &out2))
        .await??;
    ctrl.toast(format!("Exported to {}", out.display()));
    Ok(())
}

pub fn refresh_versions(ctrl: &Ctrl, filter: String) {
    let Some(ui) = ctrl.ui.upgrade() else { return };
    let app = ui.global::<App>();
    let (snapshots, old) = (app.get_show_snapshots(), app.get_show_old());
    let cached = ctrl.shared.manifest.lock().unwrap().clone();
    if cached.is_none() {
        app.set_versions_loading(true);
    }
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let manifest = match cached {
            Some(m) => m,
            None => match ctrl.shared.launcher.meta.manifest().await {
                Ok(m) => {
                    let m = Arc::new(m);
                    *ctrl.shared.manifest.lock().unwrap() = Some(m.clone());
                    m
                }
                Err(e) => {
                    ctrl.error(format!("Could not load the version list: {e}"));
                    ctrl.with_ui(|ui| ui.global::<App>().set_versions_loading(false));
                    return;
                }
            },
        };
        let needle = filter.trim().to_lowercase();
        let items: Vec<VersionItem> = manifest
            .versions
            .iter()
            .filter(|v| match v.kind {
                VersionType::Release => true,
                VersionType::Snapshot => snapshots,
                VersionType::OldBeta | VersionType::OldAlpha => old,
                VersionType::Other => false,
            })
            .filter(|v| needle.is_empty() || v.id.to_lowercase().contains(&needle))
            .map(|v| VersionItem {
                id: v.id.clone().into(),
                kind: match v.kind {
                    VersionType::Release => "Release",
                    VersionType::Snapshot => "Snapshot",
                    VersionType::OldBeta => "Beta",
                    VersionType::OldAlpha => "Alpha",
                    VersionType::Other => "",
                }
                .into(),
                date: v.release_time.format("%Y-%m-%d").to_string().into(),
            })
            .collect();
        ctrl.with_ui(move |ui| {
            let app = ui.global::<App>();
            app.set_versions(ModelRc::new(VecModel::from(items)));
            app.set_versions_loading(false);
        });
    });
}
