// No console window for release builds on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app;
mod format;
mod images;
mod keychain;
mod settings;
mod single_instance;
mod sys;

use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Context;
use slint::ComponentHandle;
use vanta_core::accounts::AccountManager;
use vanta_core::auth::AuthClient;
use vanta_core::launcher::Launcher;
use vanta_core::paths::Paths;

slint::include_modules!();

/// Azure application ID for Microsoft sign-in, provided at build time.
pub const MSA_CLIENT_ID: Option<&str> = option_env!("VANTA_MSA_CLIENT_ID");

fn init_logging(dir: &std::path::Path) -> Option<tracing_appender::non_blocking::WorkerGuard> {
    use tracing_subscriber::EnvFilter;
    std::fs::create_dir_all(dir).ok()?;
    let appender = tracing_appender::rolling::Builder::new()
        .rotation(tracing_appender::rolling::Rotation::DAILY)
        .filename_prefix("vanta")
        .filename_suffix("log")
        .max_log_files(7)
        .build(dir)
        .ok()?;
    let (writer, guard) = tracing_appender::non_blocking(appender);
    let filter = EnvFilter::try_from_env("VANTA_LOG").unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(writer)
        .with_ansi(false)
        .init();
    Some(guard)
}

fn main() -> anyhow::Result<()> {
    let started = Instant::now();
    let settings = settings::Settings::load();

    // After a self-update the old process may still hold the lock briefly.
    let mut guard = single_instance::acquire(&settings::config_dir());
    if std::env::var_os("VANTA_RESTARTED").is_some() {
        for _ in 0..50 {
            if matches!(guard, single_instance::Guard::Primary { .. }) {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
            guard = single_instance::acquire(&settings::config_dir());
        }
    }
    let single_instance::Guard::Primary {
        _lock: _lock_file,
        listener,
    } = guard
    else {
        return Ok(());
    };

    let paths = Paths::new(settings.data_root());
    let _log_guard = init_logging(&paths.logs_dir());
    tracing::info!(version = env!("CARGO_PKG_VERSION"), data = %paths.root().display(), "starting");

    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .thread_name("vanta-worker")
        .enable_all()
        .build()
        .context("could not start the async runtime")?;

    let ui = AppWindow::new().context("could not create the window")?;

    let launcher = Launcher::new(paths.clone());
    let auth = AuthClient::new(launcher.client.clone(), MSA_CLIENT_ID.unwrap_or_default());
    let accounts = AccountManager::load(paths.accounts_file(), Arc::new(keychain::Keychain), auth);
    let shared = Arc::new(app::Shared::new(launcher, accounts, settings));
    let _ctrl = app::setup(&ui, shared, rt.handle().clone());

    let weak = ui.as_weak();
    single_instance::listen(listener, move || {
        let _ = weak.upgrade_in_event_loop(|ui| app::show_window(&ui));
    });

    // Report time to first frame; `VANTA_BENCH_STARTUP=1` exits right after it.
    let bench = std::env::var_os("VANTA_BENCH_STARTUP").is_some();
    let mut reported = false;
    ui.window()
        .set_rendering_notifier(move |state, _| {
            if !reported && matches!(state, slint::RenderingState::AfterRendering) {
                reported = true;
                let ms = started.elapsed().as_secs_f64() * 1000.0;
                tracing::info!(ms, "first frame");
                if bench {
                    println!("first_frame_ms={ms:.1}");
                    let _ = slint::quit_event_loop();
                }
            }
        })
        .ok();

    ui.show()?;
    slint::run_event_loop_until_quit()?;
    let _ = ui.hide();
    rt.shutdown_timeout(Duration::from_secs(2));
    Ok(())
}
