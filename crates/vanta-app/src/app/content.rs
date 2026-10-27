//! Installed mods, resource packs and shaders of the selected instance.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};

use slint::{ComponentHandle, Image, ModelRc, VecModel};
use vanta_core::content::{Content, ContentEntry, ContentKind, ContentMeta};
use vanta_core::download::Progress;
use vanta_core::instance::{Instance, LoaderKind};
use vanta_modrinth::Version;

use super::Ctrl;
use crate::images::Pixels;
use crate::{App, AppWindow, ContentItem};

#[derive(Default)]
pub struct ContentState {
    pub instance_id: String,
    pub kind: Option<ContentKind>,
    pub entries: Vec<ContentEntry>,
    pub icons: HashMap<String, Pixels>,
    /// file name → newer version
    pub updates: HashMap<String, Version>,
    pub updating: HashSet<String>,
}

static SERIAL: AtomicU64 = AtomicU64::new(0);

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();

    app.on_refresh_content({
        let ctrl = ctrl.clone();
        move || {
            let Some(ui) = ctrl.ui.upgrade() else { return };
            let app = ui.global::<App>();
            let id = app.get_selected_id().to_string();
            let kind = ContentKind::from_index(app.get_content_kind() as usize);
            app.set_content_loading(true);
            {
                let mut s = ctrl.shared.content.lock().unwrap();
                if s.instance_id != id || s.kind != Some(kind) {
                    s.updates.clear();
                }
            }
            refresh(&ctrl, id, kind);
        }
    });

    app.on_toggle_content({
        let ctrl = ctrl.clone();
        move |file, enabled| {
            with_entry(&ctrl, &file, move |c, e| c.set_enabled(e, enabled));
        }
    });

    app.on_remove_content({
        let ctrl = ctrl.clone();
        move |file| {
            with_entry(&ctrl, &file, |c, e| c.remove(e));
        }
    });

    app.on_check_content_updates({
        let ctrl = ctrl.clone();
        move || check_updates(&ctrl, true)
    });

    app.on_update_content({
        let ctrl = ctrl.clone();
        move |file| update(&ctrl, vec![file.to_string()])
    });

    app.on_update_all_content({
        let ctrl = ctrl.clone();
        move || {
            let files: Vec<String> = ctrl
                .shared
                .content
                .lock()
                .unwrap()
                .updates
                .keys()
                .cloned()
                .collect();
            update(&ctrl, files)
        }
    });

    app.on_add_local_content({
        let ctrl = ctrl.clone();
        move || {
            let (id, kind) = current(&ctrl);
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let exts: &[&str] = match kind {
                    ContentKind::Mod => &["jar"],
                    _ => &["zip"],
                };
                let Some(files) = rfd::AsyncFileDialog::new()
                    .add_filter("Files", exts)
                    .pick_files()
                    .await
                else {
                    return;
                };
                let dir = ctrl.shared.launcher.instances.game_dir(&id).join(kind.dir_name());
                let _ = std::fs::create_dir_all(&dir);
                let mut n = 0;
                for f in files {
                    if let Some(name) = f.path().file_name()
                        && std::fs::copy(f.path(), dir.join(name)).is_ok()
                    {
                        n += 1;
                    }
                }
                ctrl.toast(format!("Added {n} file{}", if n == 1 { "" } else { "s" }));
                refresh(&ctrl, id, kind);
            });
        }
    });
}

fn current(ctrl: &Ctrl) -> (String, ContentKind) {
    let s = ctrl.shared.content.lock().unwrap();
    (s.instance_id.clone(), s.kind.unwrap_or(ContentKind::Mod))
}

fn with_entry(
    ctrl: &Ctrl,
    file: &str,
    f: impl FnOnce(&Content, &ContentEntry) -> vanta_core::Result<()> + Send + 'static,
) {
    let (id, kind) = current(ctrl);
    let entry = ctrl
        .shared
        .content
        .lock()
        .unwrap()
        .entries
        .iter()
        .find(|e| e.file_name == file)
        .cloned();
    let Some(entry) = entry else { return };
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let store = ctrl.shared.launcher.instances.clone();
        let id2 = id.clone();
        let res = tokio::task::spawn_blocking(move || {
            let (dir, game) = (store.dir(&id2), store.game_dir(&id2));
            f(
                &Content {
                    instance_dir: &dir,
                    game_dir: &game,
                },
                &entry,
            )
        })
        .await;
        if let Ok(Err(e)) = res {
            ctrl.error(e);
        }
        refresh(&ctrl, id, kind);
    });
}

pub fn refresh(ctrl: &Ctrl, id: String, kind: ContentKind) {
    if id.is_empty() {
        return;
    }
    let serial = SERIAL.fetch_add(1, Ordering::Relaxed) + 1;
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let store = ctrl.shared.launcher.instances.clone();
        let id2 = id.clone();
        let entries = tokio::task::spawn_blocking(move || {
            let (dir, game) = (store.dir(&id2), store.game_dir(&id2));
            Content {
                instance_dir: &dir,
                game_dir: &game,
            }
            .list(kind)
        })
        .await
        .unwrap_or_default();
        {
            let mut s = ctrl.shared.content.lock().unwrap();
            s.instance_id = id.clone();
            s.kind = Some(kind);
            s.entries = entries.clone();
        }
        push(&ctrl);

        // Fill in titles and icons for files Vanta did not install itself.
        enrich(&ctrl, &id, kind, &entries).await;
        if SERIAL.load(Ordering::Relaxed) != serial {
            return;
        }
        let urls: Vec<(String, String)> = {
            let s = ctrl.shared.content.lock().unwrap();
            s.entries
                .iter()
                .filter_map(|e| Some((e.file_name.clone(), e.meta.as_ref()?.icon_url.clone()?)))
                .filter(|(f, _)| !s.icons.contains_key(f))
                .collect()
        };
        for (file, url) in urls {
            if let Some(px) = ctrl.shared.images.icon(&url).await {
                ctrl.shared.content.lock().unwrap().icons.insert(file, px);
            }
        }
        push(&ctrl);
    });
}

async fn enrich(ctrl: &Ctrl, id: &str, kind: ContentKind, entries: &[ContentEntry]) {
    let unknown: Vec<(ContentEntry, String)> = entries
        .iter()
        .filter(|e| e.meta.as_ref().is_none_or(|m| m.project_id.is_empty()) && e.path.is_file())
        .filter_map(|e| Some((e.clone(), vanta_core::download::sha1_file_blocking(&e.path).ok()?)))
        .collect();
    if unknown.is_empty() {
        return;
    }
    let hashes: Vec<String> = unknown.iter().map(|(_, h)| h.clone()).collect();
    let Ok(found) = ctrl.shared.modrinth.versions_by_hashes(&hashes).await else {
        return;
    };
    let ids: Vec<String> = found
        .values()
        .map(|v| v.project_id.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    let projects: HashMap<String, vanta_modrinth::Project> = ctrl
        .shared
        .modrinth
        .projects(&ids)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|p| (p.id.clone(), p))
        .collect();
    let store = ctrl.shared.launcher.instances.clone();
    let (dir, game) = (store.dir(id), store.game_dir(id));
    let content = Content {
        instance_dir: &dir,
        game_dir: &game,
    };
    let mut changed = false;
    for (entry, sha1) in unknown {
        let Some(v) = found.get(&sha1) else { continue };
        let project = projects.get(&v.project_id);
        let meta = ContentMeta {
            project_id: v.project_id.clone(),
            version_id: v.id.clone(),
            title: project
                .map(|p| p.title.clone())
                .unwrap_or_else(|| entry.file_name.clone()),
            version_number: v.version_number.clone(),
            icon_url: project.and_then(|p| p.icon_url.clone()),
            sha1,
        };
        changed |= content.record(kind, &entry.file_name, meta).is_ok();
    }
    if changed {
        let list = content.list(kind);
        ctrl.shared.content.lock().unwrap().entries = list;
    }
}

struct Row {
    file: String,
    title: String,
    subtitle: String,
    icon: Option<Pixels>,
    enabled: bool,
    has_update: bool,
    updating: bool,
}

fn push(ctrl: &Ctrl) {
    let (rows, n_updates): (Vec<Row>, usize) = {
        let s = ctrl.shared.content.lock().unwrap();
        let rows = s
            .entries
            .iter()
            .map(|e| {
                let version = e
                    .meta
                    .as_ref()
                    .map(|m| m.version_number.clone())
                    .filter(|v| !v.is_empty());
                let mut subtitle = match version {
                    Some(v) => format!("{v} · {}", e.file_name),
                    None => e.file_name.clone(),
                };
                if let Some(u) = s.updates.get(&e.file_name) {
                    subtitle = format!("{subtitle}  →  {}", u.version_number);
                }
                Row {
                    file: e.file_name.clone(),
                    title: e.display_name().to_owned(),
                    subtitle,
                    icon: s.icons.get(&e.file_name).cloned(),
                    enabled: e.enabled,
                    has_update: s.updates.contains_key(&e.file_name),
                    updating: s.updating.contains(&e.file_name),
                }
            })
            .collect();
        (rows, s.updates.len())
    };
    ctrl.with_ui(move |ui| {
        let items: Vec<ContentItem> = rows
            .into_iter()
            .map(|r| ContentItem {
                file_name: r.file.into(),
                title: r.title.into(),
                subtitle: r.subtitle.into(),
                has_icon: r.icon.is_some(),
                icon: r.icon.map(Image::from_rgba8).unwrap_or_default(),
                enabled: r.enabled,
                has_update: r.has_update,
                updating: r.updating,
            })
            .collect();
        let app = ui.global::<App>();
        app.set_content(ModelRc::new(VecModel::from(items)));
        app.set_content_loading(false);
        app.set_updates_available(n_updates as i32);
    });
}

pub fn loaders_for(inst: &Instance, kind: ContentKind) -> Vec<&'static str> {
    match kind {
        ContentKind::Mod => inst.loader.modrinth_id().into_iter().collect(),
        ContentKind::ResourcePack => vec!["minecraft"],
        ContentKind::ShaderPack => match inst.loader {
            LoaderKind::Vanilla => vec!["vanilla"],
            _ => vec!["iris", "optifine", "canvas", "vanilla"],
        },
    }
}

fn check_updates(ctrl: &Ctrl, announce: bool) {
    let (id, kind) = current(ctrl);
    ctrl.with_ui(|ui| ui.global::<App>().set_checking_updates(true));
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let result = async {
            let inst = ctrl.shared.launcher.instances.get(&id)?;
            let by_hash: HashMap<String, String> = ctrl
                .shared
                .content
                .lock()
                .unwrap()
                .entries
                .iter()
                .filter_map(|e| Some((e.meta.as_ref()?.sha1.clone(), e.file_name.clone())))
                .collect();
            let hashes: Vec<String> = by_hash.keys().cloned().collect();
            let loaders = loaders_for(&inst, kind);
            let latest = ctrl
                .shared
                .modrinth
                .latest_versions(&hashes, &loaders, &[inst.game_version.as_str()])
                .await?;
            let updates: HashMap<String, Version> = latest
                .into_iter()
                .filter(|(sha1, v)| v.primary_file().is_some_and(|f| &f.hashes.sha1 != sha1))
                .filter_map(|(sha1, v)| Some((by_hash.get(&sha1)?.clone(), v)))
                .collect();
            anyhow::Ok(updates)
        }
        .await;
        match result {
            Ok(updates) => {
                let n = updates.len();
                ctrl.shared.content.lock().unwrap().updates = updates;
                if announce {
                    ctrl.toast(match n {
                        0 => "Everything is up to date".to_owned(),
                        1 => "1 update available".to_owned(),
                        n => format!("{n} updates available"),
                    });
                }
            }
            Err(e) => ctrl.error(format!("Could not check for updates: {e}")),
        }
        ctrl.with_ui(|ui| ui.global::<App>().set_checking_updates(false));
        push(&ctrl);
    });
}

fn update(ctrl: &Ctrl, files: Vec<String>) {
    let (id, kind) = current(ctrl);
    let jobs: Vec<(String, Version, Option<ContentMeta>)> = {
        let mut s = ctrl.shared.content.lock().unwrap();
        let jobs: Vec<_> = files
            .iter()
            .filter_map(|f| {
                let v = s.updates.get(f)?.clone();
                let meta = s
                    .entries
                    .iter()
                    .find(|e| &e.file_name == f)
                    .and_then(|e| e.meta.clone());
                Some((f.clone(), v, meta))
            })
            .collect();
        for (f, _, _) in &jobs {
            s.updating.insert(f.clone());
        }
        jobs
    };
    push(ctrl);
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let store = ctrl.shared.launcher.instances.clone();
        let (dir, game) = (store.dir(&id), store.game_dir(&id));
        let content = Content {
            instance_dir: &dir,
            game_dir: &game,
        };
        let mut done = 0;
        for (file, version, old) in jobs {
            let Some(f) = version.primary_file() else { continue };
            let meta = ContentMeta {
                project_id: version.project_id.clone(),
                version_id: version.id.clone(),
                title: old
                    .as_ref()
                    .map(|m| m.title.clone())
                    .unwrap_or_else(|| f.filename.clone()),
                version_number: version.version_number.clone(),
                icon_url: old.and_then(|m| m.icon_url),
                sha1: f.hashes.sha1.clone(),
            };
            let l = &ctrl.shared.launcher;
            let res = content
                .install(
                    &l.paths,
                    &l.downloader,
                    kind,
                    &f.url,
                    &f.filename,
                    Some(f.size),
                    meta,
                    &Progress::default(),
                )
                .await;
            let mut s = ctrl.shared.content.lock().unwrap();
            s.updating.remove(&file);
            match res {
                Ok(_) => {
                    s.updates.remove(&file);
                    done += 1;
                    // Old file of the same project is removed by `install`,
                    // except when only the name stayed the same.
                }
                Err(e) => ctrl.error(format!("Could not update {file}: {e}")),
            }
        }
        if done > 0 {
            ctrl.toast(format!("Updated {done} item{}", if done == 1 { "" } else { "s" }));
        }
        refresh(&ctrl, id, kind);
    });
}
