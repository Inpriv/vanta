//! Browse page: Modrinth search and installs.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};

use slint::{ComponentHandle, Image, Model, ModelRc, SharedString, VecModel};
use vanta_core::content::{Content, ContentKind, ContentMeta};
use vanta_core::download::{DownloadTask, Progress};
use vanta_modrinth::{SearchHit, SearchQuery, SortIndex};

use super::{Ctrl, content, instances};
use crate::format;
use crate::{App, AppWindow, ProjectItem};

const PAGE: u32 = 20;

#[derive(Default)]
pub struct BrowseState {
    query: SearchQuery,
    target: String,
    total: u32,
    hits: Vec<SearchHit>,
    installed: HashSet<String>,
    installing: HashSet<String>,
    categories: Vec<vanta_modrinth::Category>,
}

static SERIAL: AtomicU64 = AtomicU64::new(0);

thread_local! {
    static MODEL: Rc<VecModel<ProjectItem>> = Rc::new(VecModel::default());
    static ICONS: RefCell<HashMap<String, Image>> = RefCell::new(HashMap::new());
}

fn project_type(kind: i32) -> &'static str {
    match kind {
        1 => "resourcepack",
        2 => "shader",
        3 => "modpack",
        _ => "mod",
    }
}

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();
    app.set_projects(MODEL.with(|m| ModelRc::from(m.clone())));

    app.on_search({
        let ctrl = ctrl.clone();
        move || search(&ctrl, false)
    });
    app.on_load_more({
        let ctrl = ctrl.clone();
        move || search(&ctrl, true)
    });
    app.on_open_project({
        let ctrl = ctrl.clone();
        move |id| {
            let ty = ctrl
                .shared
                .browse
                .lock()
                .unwrap()
                .query
                .project_type
                .clone()
                .unwrap_or_else(|| "mod".into());
            let _ = open::that_detached(format!("https://modrinth.com/{ty}/{id}"));
        }
    });
    app.on_install_project({
        let ctrl = ctrl.clone();
        move |id| install(&ctrl, id.to_string())
    });
}

fn search(ctrl: &Ctrl, more: bool) {
    let Some(ui) = ctrl.ui.upgrade() else { return };
    let app = ui.global::<App>();
    let kind = app.get_browse_kind();
    let target = app.get_browse_target_id().to_string();

    let serial = SERIAL.fetch_add(1, Ordering::Relaxed) + 1;
    let (offset, categories_loaded) = {
        let s = ctrl.shared.browse.lock().unwrap();
        (
            if more { s.hits.len() as u32 } else { 0 },
            !s.categories.is_empty(),
        )
    };
    if !more {
        MODEL.with(|m| m.set_vec(Vec::new()));
        app.set_projects_total("".into());
    }
    app.set_projects_loading(true);
    app.set_browse_error("".into());

    let category_index = app.get_browse_category();
    let mut q = SearchQuery {
        query: app.get_browse_query().to_string(),
        project_type: Some(project_type(kind).into()),
        index: match app.get_browse_sort() {
            1 => SortIndex::Downloads,
            2 => SortIndex::Follows,
            3 => SortIndex::Newest,
            4 => SortIndex::Updated,
            _ => SortIndex::Relevance,
        },
        offset,
        limit: PAGE,
        ..Default::default()
    };

    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        if !categories_loaded && let Ok(c) = ctrl.shared.modrinth.categories().await {
            ctrl.shared.browse.lock().unwrap().categories = c;
        }
        let names = category_names(&ctrl, kind);
        if category_index > 0
            && let Some(c) = names.get(category_index as usize - 1)
        {
            q.categories = vec![c.clone()];
        }

        // Filter by the target instance's version and loader.
        let mut installed = HashSet::new();
        if kind != 3
            && let Ok(inst) = ctrl.shared.launcher.instances.get(&target)
        {
            q.game_version = Some(inst.game_version.clone());
            if kind == 0 {
                q.loader = inst.loader.modrinth_id().map(str::to_owned);
            }
            let store = &ctrl.shared.launcher.instances;
            let (dir, game) = (store.dir(&target), store.game_dir(&target));
            installed = Content {
                instance_dir: &dir,
                game_dir: &game,
            }
            .installed_projects()
            .into_keys()
            .collect();
        }

        let result = ctrl.shared.modrinth.search(&q).await;
        if SERIAL.load(Ordering::Relaxed) != serial {
            return;
        }
        let (hits, total) = match result {
            Ok(r) => (r.hits, r.total_hits),
            Err(e) => {
                let msg = format!("Search failed: {e}");
                ctrl.with_ui(move |ui| {
                    let app = ui.global::<App>();
                    app.set_browse_error(msg.into());
                    app.set_projects_loading(false);
                });
                return;
            }
        };
        let start = {
            let mut s = ctrl.shared.browse.lock().unwrap();
            if !more {
                s.hits.clear();
            }
            let start = s.hits.len();
            s.hits.extend(hits.iter().cloned());
            s.total = total;
            s.query = q;
            s.target = target;
            s.installed = installed;
            start
        };
        let more_left = (start + hits.len()) < total as usize;
        let page = hits.clone();
        let shared = ctrl.shared.clone();
        let cat_names: Vec<SharedString> = std::iter::once("All categories".to_owned())
            .chain(names.iter().map(|n| title_case(n)))
            .map(Into::into)
            .collect();
        ctrl.with_ui(move |ui| {
            let app = ui.global::<App>();
            let s = shared.browse.lock().unwrap();
            MODEL.with(|m| {
                for h in &page {
                    m.push(item(h, &s));
                }
            });
            drop(s);
            app.set_projects_more(more_left);
            app.set_projects_loading(false);
            app.set_projects_total(
                if total == 0 {
                    String::new()
                } else {
                    format!("{} results", format::grouped(total as u64))
                }
                .into(),
            );
            app.set_browse_categories(ModelRc::new(VecModel::from(cat_names)));
        });

        // Icons stream in after the text.
        for (i, h) in hits.into_iter().enumerate() {
            let Some(url) = h.icon_url.clone().filter(|u| !u.is_empty()) else {
                continue;
            };
            let ctrl2 = ctrl.clone();
            ctrl.spawn(async move {
                let Some(px) = ctrl2.shared.images.icon(&url).await else {
                    return;
                };
                ctrl2.with_ui(move |_| {
                    let img = Image::from_rgba8(px);
                    ICONS.with(|c| c.borrow_mut().insert(h.project_id.clone(), img.clone()));
                    if SERIAL.load(Ordering::Relaxed) != serial {
                        return;
                    }
                    MODEL.with(|m| {
                        let row = start + i;
                        if let Some(mut it) = m.row_data(row)
                            && it.id == h.project_id.as_str()
                        {
                            it.icon = img;
                            it.has_icon = true;
                            m.set_row_data(row, it);
                        }
                    });
                });
            });
        }
    });
}

fn category_names(ctrl: &Ctrl, kind: i32) -> Vec<String> {
    let ty = project_type(kind);
    let s = ctrl.shared.browse.lock().unwrap();
    let mut names: Vec<String> = s
        .categories
        .iter()
        .filter(|c| c.project_type == ty && c.header != "resolutions" && c.header != "performance impact")
        .map(|c| c.name.clone())
        .collect();
    names.sort();
    names.dedup();
    names
}

fn title_case(s: &str) -> String {
    s.split('-')
        .map(|w| {
            let mut c = w.chars();
            c.next()
                .map(|f| f.to_uppercase().chain(c).collect::<String>())
                .unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn item(h: &SearchHit, s: &BrowseState) -> ProjectItem {
    let cats = if h.display_categories.is_empty() {
        &h.categories
    } else {
        &h.display_categories
    };
    let icon = ICONS.with(|c| c.borrow().get(&h.project_id).cloned());
    ProjectItem {
        id: h.project_id.clone().into(),
        title: h.title.clone().into(),
        author: h.author.clone().into(),
        description: h.description.clone().into(),
        downloads: format::count(h.downloads).into(),
        categories: cats
            .iter()
            .take(3)
            .map(|c| title_case(c))
            .collect::<Vec<_>>()
            .join(" · ")
            .into(),
        has_icon: icon.is_some(),
        icon: icon.unwrap_or_default(),
        installed: s.installed.contains(&h.project_id),
        installing: s.installing.contains(&h.project_id),
    }
}

/// Re-render rows after install state changed.
fn refresh_rows(ctrl: &Ctrl) {
    let ctrl2 = ctrl.clone();
    ctrl.with_ui(move |_| {
        let s = ctrl2.shared.browse.lock().unwrap();
        MODEL.with(|m| {
            for (i, h) in s.hits.iter().enumerate() {
                if let Some(mut it) = m.row_data(i) {
                    let fresh = item(h, &s);
                    if it.installed != fresh.installed || it.installing != fresh.installing {
                        it.installed = fresh.installed;
                        it.installing = fresh.installing;
                        m.set_row_data(i, it);
                    }
                }
            }
        });
    });
}

fn install(ctrl: &Ctrl, project_id: String) {
    let (target, kind_str) = {
        let mut s = ctrl.shared.browse.lock().unwrap();
        if !s.installing.insert(project_id.clone()) {
            return;
        }
        (s.target.clone(), s.query.project_type.clone().unwrap_or_default())
    };
    refresh_rows(ctrl);
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let result = if kind_str == "modpack" {
            install_pack(&ctrl, &project_id).await
        } else {
            install_content(&ctrl, &project_id, &target, &kind_str).await
        };
        {
            let mut s = ctrl.shared.browse.lock().unwrap();
            s.installing.remove(&project_id);
            if result.is_ok() && kind_str != "modpack" {
                s.installed.insert(project_id.clone());
            }
        }
        match result {
            Ok(msg) => ctrl.toast(msg),
            Err(e) => ctrl.error(format!("Install failed: {e}")),
        }
        refresh_rows(&ctrl);
    });
}

async fn install_content(ctrl: &Ctrl, project_id: &str, target: &str, ty: &str) -> anyhow::Result<String> {
    let inst = ctrl.shared.launcher.instances.get(target)?;
    let kind = match ty {
        "resourcepack" => ContentKind::ResourcePack,
        "shader" => ContentKind::ShaderPack,
        _ => ContentKind::Mod,
    };
    let loaders = content::loaders_for(&inst, kind);
    let mr = &ctrl.shared.modrinth;
    let root = mr.best_version(project_id, &loaders, &inst.game_version).await?;

    let store = &ctrl.shared.launcher.instances;
    let (dir, game) = (store.dir(target), store.game_dir(target));
    let content = Content {
        instance_dir: &dir,
        game_dir: &game,
    };
    let installed: HashSet<String> = content.installed_projects().into_keys().collect();
    let versions = if kind == ContentKind::Mod {
        mr.resolve_with_dependencies(root, &loaders, &inst.game_version, &installed)
            .await?
    } else {
        vec![root]
    };

    let ids: Vec<String> = versions.iter().map(|v| v.project_id.clone()).collect();
    let projects: HashMap<String, vanta_modrinth::Project> = mr
        .projects(&ids)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(|p| (p.id.clone(), p))
        .collect();

    let l = &ctrl.shared.launcher;
    let mut title = String::new();
    for v in &versions {
        let Some(f) = v.primary_file() else { continue };
        let p = projects.get(&v.project_id);
        let meta = ContentMeta {
            project_id: v.project_id.clone(),
            version_id: v.id.clone(),
            title: p.map(|p| p.title.clone()).unwrap_or_else(|| f.filename.clone()),
            version_number: v.version_number.clone(),
            icon_url: p.and_then(|p| p.icon_url.clone()),
            sha1: f.hashes.sha1.clone(),
        };
        if title.is_empty() {
            title = meta.title.clone();
        }
        content
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
            .await?;
    }
    // Refresh the instance page if it shows this instance.
    ctrl.with_ui({
        let target = target.to_owned();
        move |ui| {
            let app = ui.global::<App>();
            if app.get_selected_id() == target.as_str() {
                app.invoke_refresh_content();
            }
        }
    });
    let deps = versions.len().saturating_sub(1);
    Ok(match deps {
        0 => format!("Installed {title} into {}", inst.name),
        1 => format!("Installed {title} and 1 dependency into {}", inst.name),
        n => format!("Installed {title} and {n} dependencies into {}", inst.name),
    })
}

async fn install_pack(ctrl: &Ctrl, project_id: &str) -> anyhow::Result<String> {
    let mr = &ctrl.shared.modrinth;
    let versions = mr.project_versions(project_id, &[], &[]).await?;
    let v = versions
        .iter()
        .find(|v| v.version_type == "release")
        .or(versions.first())
        .ok_or_else(|| anyhow::anyhow!("this modpack has no versions"))?;
    let f = v
        .files
        .iter()
        .find(|f| f.filename.ends_with(".mrpack"))
        .ok_or_else(|| anyhow::anyhow!("no .mrpack file in the latest version"))?;
    let l = &ctrl.shared.launcher;
    let path = l
        .paths
        .cache_dir()
        .join("modpacks")
        .join(&f.hashes.sha1)
        .with_extension("mrpack");
    l.downloader
        .fetch(
            &DownloadTask::new(&f.url, &path)
                .sha1(Some(f.hashes.sha1.clone()))
                .size(Some(f.size)),
            &Progress::default(),
        )
        .await?;
    instances::import_pack(ctrl, path).await;
    Ok(format!("Installing {}", v.name))
}
