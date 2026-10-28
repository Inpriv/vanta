//! Glue between the Slint UI and `vanta-core`.
//!
//! Rule of thumb: UI callbacks run on the UI thread and must return quickly.
//! Anything touching disk or network is spawned on the Tokio runtime, and
//! results come back through [`Ctrl::with_ui`].

mod accounts;
mod browse;
mod console;
mod content;
mod instances;
mod launch;
mod prefs;
mod update;
mod window;

use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex};

use slint::ComponentHandle;
use vanta_core::accounts::AccountManager;
use vanta_core::launcher::Launcher;
use vanta_core::version::VersionManifest;

use crate::images::ImageCache;
use crate::settings::Settings;
use crate::{App, AppWindow};

pub use window::show_window;

/// State shared between the UI thread and background tasks.
pub struct Shared {
    pub launcher: Launcher,
    pub modrinth: vanta_modrinth::Client,
    pub accounts: tokio::sync::Mutex<AccountManager>,
    pub settings: Mutex<Settings>,
    pub images: ImageCache,
    pub logs: Mutex<console::LogBuffer>,
    pub launch: Mutex<launch::LaunchState>,
    pub running: Mutex<HashMap<String, launch::Running>>,
    pub login_task: Mutex<Option<tokio::task::AbortHandle>>,
    pub manifest: Mutex<Option<Arc<VersionManifest>>>,
    pub browse: Mutex<browse::BrowseState>,
    pub content: Mutex<content::ContentState>,
    pub update: Mutex<Option<update::Release>>,
    pub quit_when_idle: std::sync::atomic::AtomicBool,
}

impl Shared {
    pub fn new(launcher: Launcher, accounts: AccountManager, settings: Settings) -> Self {
        let client = launcher.client.clone();
        Self {
            modrinth: vanta_modrinth::Client::new(client.clone()),
            images: ImageCache::new(launcher.paths.cache_dir().join("images"), client),
            launcher,
            accounts: tokio::sync::Mutex::new(accounts),
            settings: Mutex::new(settings),
            logs: Mutex::default(),
            launch: Mutex::default(),
            running: Mutex::default(),
            login_task: Mutex::default(),
            manifest: Mutex::default(),
            browse: Mutex::default(),
            content: Mutex::default(),
            update: Mutex::default(),
            quit_when_idle: Default::default(),
        }
    }
}

#[derive(Clone)]
pub struct Ctrl {
    pub shared: Arc<Shared>,
    pub rt: tokio::runtime::Handle,
    pub ui: slint::Weak<AppWindow>,
}

impl Ctrl {
    pub fn spawn<F>(&self, fut: F) -> tokio::task::JoinHandle<()>
    where
        F: Future<Output = ()> + Send + 'static,
    {
        self.rt.spawn(fut)
    }

    /// Run `f` on the UI thread (no-op if the window is gone).
    pub fn with_ui(&self, f: impl FnOnce(AppWindow) + Send + 'static) {
        let _ = self.ui.upgrade_in_event_loop(f);
    }

    pub fn toast(&self, text: impl Into<String>) {
        self.notify(text.into(), false);
    }

    pub fn error(&self, text: impl std::fmt::Display) {
        let text = text.to_string();
        tracing::warn!("{text}");
        self.notify(text, true);
    }

    fn notify(&self, text: String, error: bool) {
        self.with_ui(move |ui| {
            let app = ui.global::<App>();
            app.set_toast_text(text.into());
            app.set_toast_error(error);
            app.set_toast_serial(app.get_toast_serial().wrapping_add(1));
        });
    }

    pub fn settings(&self) -> Settings {
        self.shared.settings.lock().unwrap().clone()
    }
}

pub fn setup(ui: &AppWindow, shared: Arc<Shared>, rt: tokio::runtime::Handle) -> Ctrl {
    let ctrl = Ctrl {
        shared,
        rt,
        ui: ui.as_weak(),
    };
    let app = ui.global::<App>();
    app.set_version(env!("CARGO_PKG_VERSION").into());
    app.set_msa_configured(crate::MSA_CLIENT_ID.is_some_and(|id| !id.is_empty()));
    app.set_offline_accounts_enabled(crate::OFFLINE_ACCOUNTS);

    window::bind(&ctrl, ui);
    prefs::bind(&ctrl, ui);
    instances::bind(&ctrl, ui);
    accounts::bind(&ctrl, ui);
    browse::bind(&ctrl, ui);
    content::bind(&ctrl, ui);
    console::bind(&ctrl, ui);
    launch::bind(&ctrl, ui);
    update::bind(&ctrl, ui);

    // Initial data, loaded after the first frame is on screen.
    instances::reload(&ctrl);
    accounts::refresh(&ctrl);
    if ctrl.settings().auto_update_check {
        update::check(&ctrl, false);
    }
    ctrl
}
