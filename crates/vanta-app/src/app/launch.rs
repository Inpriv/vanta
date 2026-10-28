//! Play button: install, launch, track the running game.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use slint::{ComponentHandle, Timer, TimerMode};
use vanta_core::accounts::SessionKind;
use vanta_core::download::Progress;
use vanta_core::launch::GameEvent;
use vanta_core::launcher::{GlobalLaunchSettings, Stage};

use super::{Ctrl, console, instances};
use crate::format;
use crate::settings::CloseBehavior;
use crate::{App, AppWindow, ProgressInfo};

#[derive(Default)]
pub struct LaunchState {
    pub active: bool,
    pub instance_id: String,
    pub stage: Option<Stage>,
    pub progress: Arc<Progress>,
    pub abort: Option<tokio::task::AbortHandle>,
}

pub struct Running {
    pub started: Instant,
    kill: Option<tokio::sync::oneshot::Sender<()>>,
}

thread_local! {
    static PROGRESS_TIMER: Rc<Timer> = Rc::new(Timer::default());
    static GAME_TIMER: Rc<Timer> = Rc::new(Timer::default());
    static SPEED: RefCell<(u64, Option<Instant>, f64)> = const { RefCell::new((0, None, 0.0)) };
}

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();

    app.on_play({
        let ctrl = ctrl.clone();
        move |id| play(&ctrl, id.to_string())
    });

    app.on_stop({
        let ctrl = ctrl.clone();
        move |id| {
            if let Some(r) = ctrl.shared.running.lock().unwrap().get_mut(id.as_str())
                && let Some(kill) = r.kill.take()
            {
                let _ = kill.send(());
            }
        }
    });

    app.on_cancel_launch({
        let ctrl = ctrl.clone();
        move || {
            let mut l = ctrl.shared.launch.lock().unwrap();
            l.progress.cancel();
            if let Some(a) = l.abort.take() {
                a.abort();
            }
            l.active = false;
        }
    });
}

fn play(ctrl: &Ctrl, id: String) {
    {
        let mut l = ctrl.shared.launch.lock().unwrap();
        if l.active {
            ctrl.toast("Another instance is starting");
            return;
        }
        if ctrl.shared.running.lock().unwrap().contains_key(&id) {
            return;
        }
        *l = LaunchState {
            active: true,
            instance_id: id.clone(),
            stage: Some(Stage::Resolving),
            progress: Progress::new(),
            abort: None,
        };
    }
    start_progress_timer(ctrl);

    let c = ctrl.clone();
    let handle = ctrl.spawn(async move {
        let result = run(&c, &id).await;
        c.shared.launch.lock().unwrap().active = false;
        if let Err(e) = result {
            c.error(format!("Could not start the game: {e}"));
        }
    });
    ctrl.shared.launch.lock().unwrap().abort = Some(handle.abort_handle());
}

async fn run(ctrl: &Ctrl, id: &str) -> anyhow::Result<()> {
    let active = {
        let accounts = ctrl.shared.accounts.lock().await;
        accounts.active().map(|a| a.uuid.clone())
    };
    let Some(uuid) = active else {
        // Not signed in yet: start sign-in instead of failing.
        ctrl.with_ui(|ui| ui.global::<App>().invoke_add_account());
        return Ok(());
    };
    let (auth, kind) = ctrl.shared.accounts.lock().await.session(&uuid).await?;
    if matches!(kind, SessionKind::Offline) {
        ctrl.toast("Microsoft is unreachable; launching without online features");
    }

    let settings = ctrl.settings();
    let global = GlobalLaunchSettings {
        memory_mb: settings.default_memory_mb,
        java_path: settings.java_path.clone(),
        jvm_args: settings.jvm_args.clone(),
    };
    let progress = ctrl.shared.launch.lock().unwrap().progress.clone();
    let shared = ctrl.shared.clone();
    let on_stage = move |s: Stage| shared.launch.lock().unwrap().stage = Some(s);
    let (mut process, _cmd) = ctrl
        .shared
        .launcher
        .launch(id, auth, &global, &progress, &on_stage)
        .await?;

    // Hand the kill switch to the UI, stream events here.
    let (kill_tx, kill_rx) = tokio::sync::oneshot::channel();
    ctrl.shared.running.lock().unwrap().insert(
        id.to_owned(),
        Running {
            started: Instant::now(),
            kill: Some(kill_tx),
        },
    );
    console::start_session(ctrl, id);
    instances::reload(ctrl);
    ctrl.with_ui(move |ui| {
        match settings.close_behavior {
            CloseBehavior::KeepOpen => {}
            CloseBehavior::Minimize => ui.window().set_minimized(true),
            CloseBehavior::Close => {
                let _ = ui.hide();
            }
        }
        start_game_timer(&ui);
    });
    if settings.close_behavior == CloseBehavior::Close {
        ctrl.shared.quit_when_idle.store(true, Ordering::Relaxed);
    }

    let mut kill_rx = Some(kill_rx);
    loop {
        tokio::select! {
            ev = process.events.recv() => match ev {
                Some(GameEvent::Log(line)) => console::push(ctrl, line),
                Some(GameEvent::Exited { code, crashed, crash_report }) => {
                    finish(ctrl, id, code, crashed, crash_report).await;
                    break;
                }
                None => break,
            },
            _ = async { kill_rx.as_mut().unwrap().await }, if kill_rx.is_some() => {
                kill_rx = None;
                process.kill();
            }
        }
    }
    Ok(())
}

async fn finish(ctrl: &Ctrl, id: &str, code: Option<i32>, crashed: bool, report: Option<std::path::PathBuf>) {
    let started = ctrl.shared.running.lock().unwrap().remove(id).map(|r| r.started);
    if let Some(started) = started {
        let secs = started.elapsed().as_secs();
        let store = ctrl.shared.launcher.instances.clone();
        let id = id.to_owned();
        let _ = tokio::task::spawn_blocking(move || {
            if let Ok(mut inst) = store.get(&id) {
                inst.play_time_secs += secs;
                let _ = store.save(&inst);
            }
        })
        .await;
    }
    console::flush_soon(ctrl);
    instances::reload(ctrl);

    let killed_by_user = code.is_none();
    if crashed && !killed_by_user {
        console::show_crash(ctrl, code, report).await;
        ctrl.with_ui(|ui| super::show_window(&ui));
    } else if ctrl.shared.quit_when_idle.load(Ordering::Relaxed)
        && ctrl.shared.running.lock().unwrap().is_empty()
    {
        let _ = slint::invoke_from_event_loop(|| {
            let _ = slint::quit_event_loop();
        });
    } else if ctrl.settings().close_behavior == CloseBehavior::Minimize {
        ctrl.with_ui(|ui| super::show_window(&ui));
    }
}

fn start_progress_timer(ctrl: &Ctrl) {
    let c = ctrl.clone();
    let ctrl2 = ctrl.clone();
    ctrl.with_ui(move |_| {
        let ctrl = ctrl2;
        SPEED.with(|s| *s.borrow_mut() = (0, None, 0.0));
        PROGRESS_TIMER.with(|t| {
            let timer = Rc::downgrade(t);
            t.start(TimerMode::Repeated, Duration::from_millis(100), move || {
                let Some(ui) = c.ui.upgrade() else { return };
                let info = snapshot(&c);
                let active = info.active;
                ui.global::<App>().set_launch_progress(info);
                if !active && let Some(t) = timer.upgrade() {
                    t.stop();
                }
            });
        });
        let l = ctrl.shared.launch.lock().unwrap();
        if let Some(ui) = ctrl.ui.upgrade() {
            ui.global::<App>()
                .set_launch_instance_id(l.instance_id.clone().into());
        }
    });
}

fn snapshot(ctrl: &Ctrl) -> ProgressInfo {
    let l = ctrl.shared.launch.lock().unwrap();
    if !l.active {
        return ProgressInfo::default();
    }
    let snap = l.progress.snapshot();
    let stage = l.stage.unwrap_or(Stage::Resolving);
    let speed = SPEED.with(|s| {
        let mut s = s.borrow_mut();
        let now = Instant::now();
        if let Some(prev) = s.1 {
            let dt = now.duration_since(prev).as_secs_f64();
            if dt > 0.0 && snap.done_bytes >= s.0 {
                let inst = (snap.done_bytes - s.0) as f64 / dt;
                s.2 = if s.2 == 0.0 { inst } else { s.2 * 0.8 + inst * 0.2 };
            }
        }
        *s = (snap.done_bytes, Some(now), s.2);
        s.2
    });
    let downloading = snap.total_files > 0 && snap.done_files < snap.total_files;
    let detail = if downloading {
        let left = snap.total_files - snap.done_files;
        format!(
            "{} of {} · {}/s · {} {} left",
            format::megabytes(snap.done_bytes),
            format::megabytes(snap.total_bytes),
            format::megabytes(speed as u64),
            format::grouped(left),
            if left == 1 { "file" } else { "files" }
        )
    } else if snap.total_files > 0 {
        format!("{} files verified", format::grouped(snap.total_files))
    } else {
        String::new()
    };
    ProgressInfo {
        active: true,
        stage: stage.label().into(),
        fraction: if downloading || snap.total_files > 0 {
            snap.fraction()
        } else {
            -1.0
        },
        detail: detail.into(),
    }
}

fn start_game_timer(ui: &AppWindow) {
    let weak = ui.as_weak();
    GAME_TIMER.with(|t| {
        let mut tick = 0u32;
        t.start(TimerMode::Repeated, Duration::from_millis(200), move || {
            let Some(ui) = weak.upgrade() else { return };
            console::flush(&ui);
            tick += 1;
            if tick.is_multiple_of(5) {
                running_time(&ui);
            }
        });
    });
    running_time(ui);
}

fn running_time(ui: &AppWindow) {
    let Some(ctrl) = super::console::ctrl() else {
        return;
    };
    let running = ctrl.shared.running.lock().unwrap();
    let app = ui.global::<App>();
    let secs = running
        .get(app.get_featured_id().as_str())
        .or_else(|| running.values().next())
        .map(|r| r.started.elapsed().as_secs());
    match secs {
        Some(s) => app.set_running_time(format::duration(s).into()),
        None => GAME_TIMER.with(|t| t.stop()),
    }
}
