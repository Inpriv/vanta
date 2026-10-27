//! Borderless window behaviour: dragging, resizing, maximize, close.

use std::sync::atomic::Ordering;

use slint::ComponentHandle;
use slint::winit_030::{WinitWindowAccessor, winit};

use super::Ctrl;
use crate::{App, AppWindow};

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();
    let weak = ui.as_weak();

    app.on_start_drag({
        let weak = weak.clone();
        move || {
            if let Some(ui) = weak.upgrade() {
                ui.window().with_winit_window(|w: &winit::window::Window| {
                    let _ = w.drag_window();
                });
            }
        }
    });

    app.on_start_resize({
        let weak = weak.clone();
        move |dir| {
            use winit::window::ResizeDirection::*;
            let dir = match dir {
                0 => East,
                1 => North,
                2 => NorthEast,
                3 => NorthWest,
                4 => South,
                5 => SouthEast,
                6 => SouthWest,
                _ => West,
            };
            if let Some(ui) = weak.upgrade() {
                ui.window().with_winit_window(|w: &winit::window::Window| {
                    let _ = w.drag_resize_window(dir);
                });
            }
        }
    });

    app.on_toggle_maximize({
        let weak = weak.clone();
        move || {
            if let Some(ui) = weak.upgrade() {
                let max = !ui.window().is_maximized();
                ui.window().set_maximized(max);
                ui.global::<App>().set_maximized(max);
            }
        }
    });

    app.on_minimize({
        let weak = weak.clone();
        move || {
            if let Some(ui) = weak.upgrade() {
                ui.window().set_minimized(true);
            }
        }
    });

    app.on_close({
        let ctrl = ctrl.clone();
        let weak = weak.clone();
        move || {
            if ctrl.shared.running.lock().unwrap().is_empty() {
                let _ = slint::quit_event_loop();
            } else {
                // Keep running (and capturing logs) until the game exits.
                ctrl.shared.quit_when_idle.store(true, Ordering::Relaxed);
                if let Some(ui) = weak.upgrade() {
                    let _ = ui.hide();
                }
            }
        }
    });

    app.on_open_url(|url| {
        if url.starts_with("https://") || url.starts_with("http://") {
            let _ = open::that_detached(url.as_str());
        }
    });

    app.on_text_matches(|text, needle| text.to_lowercase().contains(&needle.to_lowercase()));
}

/// Bring the window to the front (used by the single-instance guard).
pub fn show_window(ui: &AppWindow) {
    let _ = ui.show();
    ui.window().set_minimized(false);
    ui.window()
        .with_winit_window(|w: &winit::window::Window| w.focus_window());
}
