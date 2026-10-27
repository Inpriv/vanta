//! Microsoft sign-in and account switching.

use std::cell::RefCell;

use slint::{ComponentHandle, Image, ModelRc, VecModel};

use super::Ctrl;
use crate::images::Pixels;
use crate::{AccountItem, App, AppWindow};

thread_local! {
    // X11/Wayland clipboards only keep content while the owner is alive.
    static CLIPBOARD: RefCell<Option<arboard::Clipboard>> = const { RefCell::new(None) };
}

/// Copy text to the system clipboard (UI thread only).
pub fn copy_text(text: &str) -> bool {
    CLIPBOARD.with(|c| {
        let mut c = c.borrow_mut();
        if c.is_none() {
            *c = arboard::Clipboard::new().ok();
        }
        c.as_mut().is_some_and(|c| c.set_text(text.to_owned()).is_ok())
    })
}

pub fn bind(ctrl: &Ctrl, ui: &AppWindow) {
    let app = ui.global::<App>();

    app.on_add_account({
        let ctrl = ctrl.clone();
        move || start_login(&ctrl)
    });

    app.on_cancel_login({
        let ctrl = ctrl.clone();
        move || {
            if let Some(task) = ctrl.shared.login_task.lock().unwrap().take() {
                task.abort();
            }
            ctrl.with_ui(|ui| ui.global::<App>().set_login_open(false));
        }
    });

    app.on_copy_login_code({
        let ctrl = ctrl.clone();
        move || {
            let Some(ui) = ctrl.ui.upgrade() else { return };
            if copy_text(&ui.global::<App>().get_login_code()) {
                ctrl.toast("Code copied");
            }
        }
    });

    app.on_switch_account({
        let ctrl = ctrl.clone();
        move |uuid| {
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let res = ctrl.shared.accounts.lock().await.set_active(&uuid);
                if let Err(e) = res {
                    ctrl.error(e);
                }
                refresh(&ctrl);
            });
        }
    });

    app.on_remove_account({
        let ctrl = ctrl.clone();
        move |uuid| {
            let ctrl = ctrl.clone();
            ctrl.clone().spawn(async move {
                let res = ctrl.shared.accounts.lock().await.remove(&uuid);
                match res {
                    Ok(()) => ctrl.toast("Signed out"),
                    Err(e) => ctrl.error(e),
                }
                refresh(&ctrl);
            });
        }
    });
}

fn start_login(ctrl: &Ctrl) {
    if let Some(task) = ctrl.shared.login_task.lock().unwrap().take() {
        task.abort();
    }
    ctrl.with_ui(|ui| {
        let app = ui.global::<App>();
        app.set_login_code("".into());
        app.set_login_error("".into());
        app.set_login_open(true);
    });
    let c = ctrl.clone();
    let handle = ctrl.spawn(async move {
        let auth = c.shared.accounts.lock().await.auth().clone();
        let result = async {
            let code = auth.start_device_code().await?;
            let (user_code, uri) = (code.user_code.clone(), code.verification_uri.clone());
            c.with_ui(move |ui| {
                let app = ui.global::<App>();
                app.set_login_code(user_code.into());
                app.set_login_url(uri.into());
            });
            let tokens = auth.poll_device_code(&code).await?;
            let session = auth.login_minecraft(&tokens.access_token).await?;
            Ok::<_, vanta_core::auth::AuthError>((tokens, session))
        }
        .await;
        match result {
            Ok((tokens, session)) => {
                let added = c.shared.accounts.lock().await.add(tokens, session);
                match added {
                    Ok(account) => {
                        c.with_ui(|ui| ui.global::<App>().set_login_open(false));
                        c.toast(format!("Signed in as {}", account.name));
                        refresh(&c);
                    }
                    Err(e) => c.with_ui(move |ui| ui.global::<App>().set_login_error(e.to_string().into())),
                }
            }
            Err(e) => {
                let msg = e.to_string();
                c.with_ui(move |ui| ui.global::<App>().set_login_error(msg.into()));
            }
        }
    });
    *ctrl.shared.login_task.lock().unwrap() = Some(handle.abort_handle());
}

struct Row {
    uuid: String,
    name: String,
    head: Option<Pixels>,
    active: bool,
}

fn item(r: &Row) -> AccountItem {
    AccountItem {
        uuid: r.uuid.clone().into(),
        name: r.name.clone().into(),
        has_head: r.head.is_some(),
        head: r.head.clone().map(Image::from_rgba8).unwrap_or_default(),
        active: r.active,
    }
}

/// Push the account list (and skin heads) to the UI.
pub fn refresh(ctrl: &Ctrl) {
    let ctrl = ctrl.clone();
    ctrl.clone().spawn(async move {
        let list = ctrl.shared.accounts.lock().await.list.clone();
        let mut rows = Vec::new();
        for a in &list.accounts {
            let head = match &a.skin_url {
                Some(url) => ctrl.shared.images.head(url).await,
                None => None,
            };
            rows.push(Row {
                uuid: a.uuid.clone(),
                name: a.name.clone(),
                head,
                active: list.active.as_deref() == Some(a.uuid.as_str()),
            });
        }
        ctrl.with_ui(move |ui| {
            let app = ui.global::<App>();
            let items: Vec<AccountItem> = rows.iter().map(item).collect();
            let active = items.iter().find(|a| a.active).cloned();
            app.set_has_account(active.is_some());
            app.set_active_account(active.unwrap_or_default());
            app.set_accounts(ModelRc::new(VecModel::from(items)));
        });
    });
}
