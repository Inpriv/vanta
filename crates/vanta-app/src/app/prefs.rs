//! Settings page.

use std::path::PathBuf;

use slint::winit_030::{WinitWindowAccessor, winit};
use slint::{ComponentHandle, ModelRc, VecModel};

use super::Ctrl;
use crate::settings::{CloseBehavior, ThemeMode};
use crate::{App, AppWindow, JavaItem, Theme};

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();
    let s = ctrl.settings();
    app.set_theme_index(match s.theme {
        ThemeMode::Dark => 0,
        ThemeMode::Light => 1,
        ThemeMode::System => 2,
    });
    app.set_accent_index(s.accent as i32);
    app.set_reduce_motion(s.reduce_motion);
    app.set_default_memory_mb(s.default_memory_mb as i32);
    app.set_max_memory_mb(crate::sys::total_memory_mb().unwrap_or(16384).max(4096) as i32);
    app.set_java_path(
        s.java_path
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_default()
            .into(),
    );
    app.set_global_jvm_args(s.jvm_args.clone().into());
    app.set_close_behavior(match s.close_behavior {
        CloseBehavior::KeepOpen => 0,
        CloseBehavior::Minimize => 1,
        CloseBehavior::Close => 2,
    });
    app.set_auto_update_check(s.auto_update_check);
    app.set_data_dir(ctrl.shared.launcher.paths.root().display().to_string().into());
    apply_theme(ui);

    app.on_settings_changed({
        let ctrl = ctrl.clone();
        move || {
            let Some(ui) = ctrl.ui.upgrade() else { return };
            let app = ui.global::<App>();
            let settings = {
                let mut s = ctrl.shared.settings.lock().unwrap();
                s.theme = match app.get_theme_index() {
                    1 => ThemeMode::Light,
                    2 => ThemeMode::System,
                    _ => ThemeMode::Dark,
                };
                s.accent = app.get_accent_index().clamp(0, 5) as u8;
                s.reduce_motion = app.get_reduce_motion();
                s.default_memory_mb = app.get_default_memory_mb().max(512) as u32;
                let java = app.get_java_path().trim().to_owned();
                s.java_path = (!java.is_empty()).then(|| PathBuf::from(java));
                s.jvm_args = app.get_global_jvm_args().to_string();
                s.close_behavior = match app.get_close_behavior() {
                    1 => CloseBehavior::Minimize,
                    2 => CloseBehavior::Close,
                    _ => CloseBehavior::KeepOpen,
                };
                s.auto_update_check = app.get_auto_update_check();
                s.clone()
            };
            apply_theme(&ui);
            ctrl.spawn(async move {
                if let Err(e) = tokio::task::spawn_blocking(move || settings.save()).await {
                    tracing::warn!(error = %e, "could not save settings");
                }
            });
        }
    });

    app.on_detect_java({
        let ctrl = ctrl.clone();
        move || {
            ctrl.with_ui(|ui| ui.global::<App>().set_javas_loading(true));
            let ctrl2 = ctrl.clone();
            ctrl.spawn(async move {
                let paths = ctrl2.shared.launcher.paths.clone();
                let found = tokio::task::spawn_blocking(move || vanta_core::java::detect(&paths))
                    .await
                    .unwrap_or_default();
                let n = found.len();
                let items: Vec<JavaItem> = found
                    .into_iter()
                    .map(|j| JavaItem {
                        label: format!("Java {}{}", j.major, if j.managed { " · Vanta" } else { "" }).into(),
                        path: j.path.display().to_string().into(),
                    })
                    .collect();
                ctrl2.with_ui(move |ui| {
                    let app = ui.global::<App>();
                    app.set_javas(ModelRc::new(VecModel::from(items)));
                    app.set_javas_loading(false);
                });
                ctrl2.toast(if n == 1 {
                    "Found 1 Java installation".into()
                } else {
                    format!("Found {n} Java installations")
                });
            });
        }
    });

    app.on_browse_java({
        let ctrl = ctrl.clone();
        move || {
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                if let Some(file) = rfd::AsyncFileDialog::new()
                    .set_title("Choose a Java executable")
                    .pick_file()
                    .await
                {
                    let path = file.path().display().to_string();
                    ctrl.with_ui(move |ui| {
                        let app = ui.global::<App>();
                        app.set_java_path(path.into());
                        app.invoke_settings_changed();
                    });
                }
            });
        }
    });

    app.on_change_data_dir({
        let ctrl = ctrl.clone();
        move || {
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let Some(dir) = rfd::AsyncFileDialog::new()
                    .set_title("Choose a data folder")
                    .pick_folder()
                    .await
                else {
                    return;
                };
                let dir = dir.path().to_path_buf();
                let settings = {
                    let mut s = ctrl.shared.settings.lock().unwrap();
                    s.data_dir = Some(dir.clone());
                    s.clone()
                };
                match settings.save() {
                    Ok(()) => ctrl.toast(format!(
                        "Data folder set to {}. Restart Vanta to use it.",
                        dir.display()
                    )),
                    Err(e) => ctrl.error(format!("Could not save settings: {e}")),
                }
            });
        }
    });

    app.on_open_data_dir({
        let ctrl = ctrl.clone();
        move || {
            let _ = open::that_detached(ctrl.shared.launcher.paths.root());
        }
    });
}

/// Push the theme settings into the `Theme` global.
pub fn apply_theme(ui: &AppWindow) {
    let app = ui.global::<App>();
    let dark = match app.get_theme_index() {
        1 => false,
        2 => ui
            .window()
            .with_winit_window(|w: &winit::window::Window| w.theme())
            .flatten()
            .map(|t| t == winit::window::Theme::Dark)
            .unwrap_or(true),
        _ => true,
    };
    let theme = ui.global::<Theme>();
    theme.set_dark(dark);
    theme.set_accent_index(app.get_accent_index());
}
