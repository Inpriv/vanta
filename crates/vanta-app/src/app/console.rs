//! Live game log and crash screen.

use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;

use slint::{ComponentHandle, Model, ModelRc, VecModel};
use vanta_core::launch::{LogLevel, LogLine};

use super::Ctrl;
use crate::{App, AppWindow, LogItem};

const MAX_LINES: usize = 50_000;

#[derive(Default)]
pub struct LogBuffer {
    pub lines: Vec<LogLine>,
    /// Lines dropped from the front, so UI indices stay valid.
    pub dropped: usize,
    pub instance_id: String,
}

thread_local! {
    static MODEL: Rc<VecModel<LogItem>> = Rc::new(VecModel::default());
    /// Absolute index of the next buffer line the UI has not seen.
    static SHOWN: RefCell<usize> = const { RefCell::new(0) };
    static CTRL: RefCell<Option<Ctrl>> = const { RefCell::new(None) };
    static CRASH_REPORT: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
}

pub fn ctrl() -> Option<Ctrl> {
    CTRL.with(|c| c.borrow().clone())
}

fn level_num(l: LogLevel) -> i32 {
    match l {
        LogLevel::Trace | LogLevel::Debug => 0,
        LogLevel::Info => 1,
        LogLevel::Warn => 2,
        LogLevel::Error | LogLevel::Fatal => 3,
    }
}

fn to_item(l: &LogLine) -> LogItem {
    LogItem {
        text: l.text.replace('\t', "    ").into(),
        level: level_num(l.level),
    }
}

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    CTRL.with(|c| *c.borrow_mut() = Some(ctrl.clone()));
    let app = ui.global::<App>();
    app.set_logs(MODEL.with(|m| ModelRc::from(m.clone())));

    app.on_apply_log_filter({
        let weak = ui.as_weak();
        move || {
            if let Some(ui) = weak.upgrade() {
                rebuild(&ui);
            }
        }
    });

    app.on_clear_logs({
        let ctrl = ctrl.clone();
        move || {
            let mut b = ctrl.shared.logs.lock().unwrap();
            b.dropped += b.lines.len();
            b.lines.clear();
            SHOWN.with(|s| *s.borrow_mut() = b.dropped);
            MODEL.with(|m| m.set_vec(Vec::new()));
        }
    });

    app.on_copy_logs({
        let ctrl = ctrl.clone();
        let weak = ui.as_weak();
        move || {
            let Some(ui) = weak.upgrade() else { return };
            let (filter, search) = filters(&ui);
            let text: String = ctrl
                .shared
                .logs
                .lock()
                .unwrap()
                .lines
                .iter()
                .filter(|l| matches(l, filter, &search))
                .map(|l| l.text.as_str())
                .collect::<Vec<_>>()
                .join("\n");
            if super::accounts::copy_text(&text) {
                ctrl.toast("Log copied to the clipboard");
            }
        }
    });

    app.on_open_logs_folder({
        let ctrl = ctrl.clone();
        move || {
            let id = ctrl.shared.logs.lock().unwrap().instance_id.clone();
            let dir = if id.is_empty() {
                ctrl.shared.launcher.paths.logs_dir()
            } else {
                ctrl.shared.launcher.instances.game_dir(&id).join("logs")
            };
            let _ = std::fs::create_dir_all(&dir);
            let _ = open::that_detached(dir);
        }
    });

    app.on_open_crash_report(|| {
        CRASH_REPORT.with(|r| {
            if let Some(p) = r.borrow().as_ref() {
                let _ = open::that_detached(p);
            }
        });
    });

    app.on_copy_crash({
        let ctrl = ctrl.clone();
        let weak = ui.as_weak();
        move || {
            let Some(ui) = weak.upgrade() else { return };
            let app = ui.global::<App>();
            let text = format!("{}\n\n{}", app.get_crash_summary(), app.get_crash_detail());
            if super::accounts::copy_text(&text) {
                ctrl.toast("Copied");
            }
        }
    });
}

fn filters(ui: &AppWindow) -> (i32, String) {
    let app = ui.global::<App>();
    (app.get_log_filter(), app.get_log_search().to_lowercase())
}

fn matches(l: &LogLine, filter: i32, search: &str) -> bool {
    let lvl = level_num(l.level);
    let level_ok = match filter {
        1 => lvl >= 2,
        2 => lvl >= 3,
        _ => true,
    };
    level_ok && (search.is_empty() || l.text.to_lowercase().contains(search))
}

/// Start a fresh log for a newly launched instance.
pub fn start_session(ctrl: &Ctrl, instance_id: &str) {
    {
        let mut b = ctrl.shared.logs.lock().unwrap();
        b.dropped += b.lines.len();
        b.lines.clear();
        b.instance_id = instance_id.to_owned();
    }
    let dropped = ctrl.shared.logs.lock().unwrap().dropped;
    ctrl.with_ui(move |_| {
        SHOWN.with(|s| *s.borrow_mut() = dropped);
        MODEL.with(|m| m.set_vec(Vec::new()));
    });
}

pub fn push(ctrl: &Ctrl, line: LogLine) {
    let mut b = ctrl.shared.logs.lock().unwrap();
    b.lines.push(line);
    if b.lines.len() > MAX_LINES {
        let excess = b.lines.len() - MAX_LINES;
        b.lines.drain(..excess);
        b.dropped += excess;
    }
}

/// Append buffered lines to the UI model (UI thread).
pub fn flush(ui: &AppWindow) {
    let Some(ctrl) = ctrl() else { return };
    let (filter, search) = filters(ui);
    let b = ctrl.shared.logs.lock().unwrap();
    let total = b.dropped + b.lines.len();
    SHOWN.with(|s| {
        let mut shown = s.borrow_mut();
        if *shown >= total {
            return;
        }
        let start = shown.saturating_sub(b.dropped);
        let new: Vec<LogItem> = b.lines[start..]
            .iter()
            .filter(|l| matches(l, filter, &search))
            .map(to_item)
            .collect();
        *shown = total;
        MODEL.with(|m| {
            for item in new {
                m.push(item);
            }
            // Keep the view bounded; trim in chunks to avoid O(n²).
            if m.row_count() > 25_000 {
                let keep: Vec<LogItem> = (m.row_count() - 20_000..m.row_count())
                    .filter_map(|i| m.row_data(i))
                    .collect();
                m.set_vec(keep);
            }
        });
    });
}

pub fn flush_soon(ctrl: &Ctrl) {
    ctrl.with_ui(|ui| flush(&ui));
}

fn rebuild(ui: &AppWindow) {
    let Some(ctrl) = ctrl() else { return };
    let (filter, search) = filters(ui);
    let b = ctrl.shared.logs.lock().unwrap();
    let items: Vec<LogItem> = b
        .lines
        .iter()
        .filter(|l| matches(l, filter, &search))
        .rev()
        .take(20_000)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .map(to_item)
        .collect();
    SHOWN.with(|s| *s.borrow_mut() = b.dropped + b.lines.len());
    MODEL.with(|m| m.set_vec(items));
}

/// Best-effort explanation of a crash from the last log lines.
pub fn diagnose(lines: &[&str]) -> &'static str {
    let any = |needles: &[&str]| lines.iter().any(|l| needles.iter().any(|n| l.contains(n)));
    if any(&["java.lang.OutOfMemoryError"]) {
        "The game ran out of memory. Give it more in the instance settings."
    } else if any(&[
        "UnsupportedClassVersionError",
        "has been compiled by a more recent version of the Java Runtime",
    ]) {
        "A mod or the game needs a newer Java version. Clear the custom Java path to let Vanta choose."
    } else if any(&[
        "Incompatible mods found",
        "Mod resolution failed",
        "ModResolutionException",
        "Missing or unsupported mandatory dependencies",
    ]) {
        "Some mods are missing dependencies or are incompatible with each other."
    } else if any(&["Could not reserve enough space for", "Invalid maximum heap size"]) {
        "Java could not allocate the requested memory. Try a smaller memory setting."
    } else if any(&[
        "GLFW error",
        "Pixel format not accelerated",
        "WGL: The driver does not appear to support OpenGL",
    ]) {
        "The graphics driver could not create an OpenGL context. Updating your GPU driver usually fixes this."
    } else {
        "Check the details below or open the crash report."
    }
}

pub async fn show_crash(ctrl: &Ctrl, code: Option<i32>, report: Option<PathBuf>) {
    let tail: Vec<String> = {
        let b = ctrl.shared.logs.lock().unwrap();
        b.lines.iter().rev().take(400).map(|l| l.text.clone()).collect()
    };
    let refs: Vec<&str> = tail.iter().map(String::as_str).collect();
    let hint = diagnose(&refs);
    let detail = match &report {
        Some(p) => tokio::fs::read_to_string(p)
            .await
            .map(|t| t.lines().take(80).collect::<Vec<_>>().join("\n"))
            .unwrap_or_default(),
        None => tail.iter().take(40).rev().cloned().collect::<Vec<_>>().join("\n"),
    };
    let summary = match code {
        Some(c) => format!("Exit code {c}. {hint}"),
        None => hint.to_owned(),
    };
    let has_report = report.is_some();
    ctrl.with_ui(move |ui| {
        CRASH_REPORT.with(|r| *r.borrow_mut() = report);
        let app = ui.global::<App>();
        app.set_crash_summary(summary.into());
        app.set_crash_detail(detail.into());
        app.set_crash_has_report(has_report);
        app.set_crash_open(true);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn diagnoses_common_crashes() {
        assert!(diagnose(&["Exception: java.lang.OutOfMemoryError: Java heap space"]).contains("memory"));
        assert!(
            diagnose(&["net.fabricmc.loader.impl.FormattedException: Mod resolution failed"])
                .contains("mods")
        );
        assert!(
            diagnose(&["has been compiled by a more recent version of the Java Runtime"]).contains("Java")
        );
        assert!(diagnose(&["nothing useful"]).contains("crash report"));
    }
}
