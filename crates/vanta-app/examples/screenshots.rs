//! Renders the real UI headlessly with sample data and writes PNGs.
//! Used for the README/website screenshots and as a visual check in CI.
//!
//!     cargo run -p vanta-app --example screenshots -- <out-dir>
//!
//! Set `VANTA_SHOT_ICONS` to a folder of `<modrinth-slug>.png` files to show
//! real project icons (as the app would after fetching them); without it the
//! sample projects get plain gradients. Icons are not kept in the repo.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use slint::platform::software_renderer::{MinimalSoftwareWindow, PremultipliedRgbaColor, RepaintBufferType};
use slint::platform::{Platform, WindowAdapter, WindowEvent};
use slint::{ComponentHandle, Image, Model, ModelRc, PhysicalSize, Rgba8Pixel, SharedPixelBuffer, VecModel};

slint::include_modules!();

thread_local! {
    static CLOCK: Cell<Duration> = const { Cell::new(Duration::ZERO) };
}

struct Headless {
    window: Rc<MinimalSoftwareWindow>,
}

impl Platform for Headless {
    fn create_window_adapter(&self) -> Result<Rc<dyn WindowAdapter>, slint::PlatformError> {
        Ok(self.window.clone())
    }
    fn duration_since_start(&self) -> Duration {
        CLOCK.with(|c| c.get())
    }
}

const W: u32 = 800;
const H: u32 = 520;
const SCALE: f32 = 2.0;

fn gradient(a: [u8; 3], b: [u8; 3]) -> Image {
    let mut px = SharedPixelBuffer::<Rgba8Pixel>::new(64, 64);
    for (i, p) in px.make_mut_slice().iter_mut().enumerate() {
        let (x, y) = ((i % 64) as f32 / 63.0, (i / 64) as f32 / 63.0);
        let t = (x + y) / 2.0;
        let mix = |c: usize| (a[c] as f32 * (1.0 - t) + b[c] as f32 * t) as u8;
        *p = Rgba8Pixel::new(mix(0), mix(1), mix(2), 255);
    }
    Image::from_rgba8(px)
}

/// The project's icon from `VANTA_SHOT_ICONS`, or a gradient placeholder.
fn project_icon(slug: &str, a: [u8; 3], b: [u8; 3]) -> Image {
    std::env::var_os("VANTA_SHOT_ICONS")
        .and_then(|dir| image::open(std::path::Path::new(&dir).join(format!("{slug}.png"))).ok())
        .map(|img| {
            let img = img.to_rgba8();
            Image::from_rgba8(SharedPixelBuffer::clone_from_slice(
                img.as_raw(),
                img.width(),
                img.height(),
            ))
        })
        .unwrap_or_else(|| gradient(a, b))
}

fn head() -> Image {
    let colors = [[96, 62, 40], [120, 80, 54], [240, 200, 160], [70, 110, 200]];
    let mut px = SharedPixelBuffer::<Rgba8Pixel>::new(8, 8);
    for (i, p) in px.make_mut_slice().iter_mut().enumerate() {
        let (x, y) = (i % 8, i / 8);
        let c = if y < 2 {
            colors[0]
        } else if y == 4 && (x == 2 || x == 5) {
            colors[3]
        } else if y > 5 && x > 1 && x < 6 {
            colors[1]
        } else {
            colors[2]
        };
        *p = Rgba8Pixel::new(c[0], c[1], c[2], 255);
    }
    Image::from_rgba8(px)
}

fn inst(id: &str, name: &str, icon: &str, version: &str, loader: &str, played: &str) -> InstanceItem {
    InstanceItem {
        id: id.into(),
        name: name.into(),
        initial: name[..1].into(),
        icon: icon.into(),
        game_version: version.into(),
        loader: loader.into(),
        loader_index: ["Vanilla", "Fabric", "Quilt", "Forge", "NeoForge"]
            .iter()
            .position(|l| *l == loader)
            .unwrap_or(0) as i32,
        loader_version: "".into(),
        last_played: played.into(),
        running: false,
        memory_mb: 0,
        jvm_args: "".into(),
        java_path: "".into(),
        custom_resolution: false,
        width: 1280,
        height: 720,
    }
}

fn fill(ui: &AppWindow) {
    let app = ui.global::<App>();
    app.on_text_matches(|a, b| a.to_lowercase().contains(&b.to_lowercase()));
    app.on_initial_of(|n| {
        n.chars()
            .find(|c| c.is_alphanumeric())
            .map(|c| c.to_uppercase().collect::<String>())
            .unwrap_or_else(|| "V".into())
            .into()
    });
    let instances = vec![
        inst(
            "survival",
            "Latest 26.3",
            "grass",
            "26.3",
            "Fabric",
            "2 hours ago",
        ),
        inst("create", "Forge 1.20.1", "planks", "1.20.1", "Forge", "yesterday"),
        inst(
            "skyblock",
            "Skyblock",
            "ocean",
            "1.21.1",
            "NeoForge",
            "3 days ago",
        ),
        inst(
            "speedrun",
            "Speedrun Practice",
            "frost",
            "1.16.1",
            "Fabric",
            "1 week ago",
        ),
        inst(
            "classic",
            "Beta Nostalgia",
            "cobblestone",
            "b1.7.3",
            "Vanilla",
            "",
        ),
        inst("end", "Into the End", "end_stone", "1.21.4", "Quilt", ""),
    ];
    app.set_featured(instances[0].clone());
    app.set_featured_id("survival".into());
    app.set_has_featured(true);
    app.set_selected(instances[0].clone());
    app.set_selected_id("survival".into());
    app.set_browse_target_id("survival".into());
    app.set_browse_target_index(0);
    app.set_instance_names(ModelRc::new(VecModel::from(
        instances.iter().map(|i| i.name.clone()).collect::<Vec<_>>(),
    )));
    app.set_instances(ModelRc::new(VecModel::from(instances)));
    app.set_instances_loaded(true);

    let account = AccountItem {
        uuid: "1".into(),
        name: "Inpriv".into(),
        head: head(),
        has_head: true,
        active: true,
        offline: false,
    };
    app.set_active_account(account.clone());
    app.set_has_account(true);
    app.set_accounts(ModelRc::new(VecModel::from(vec![account])));

    let projects = [
        (
            "sodium",
            "Sodium",
            "jellysquid3",
            "A modern rendering engine that greatly improves frame rates.",
            "72.4M",
            "Optimization",
            [120, 220, 170],
            [40, 120, 90],
        ),
        (
            "iris",
            "Iris Shaders",
            "coderbot",
            "A shaders mod compatible with OptiFine shader packs.",
            "48.1M",
            "Decoration · Optimization",
            [140, 120, 255],
            [60, 40, 180],
        ),
        (
            "lithium",
            "Lithium",
            "jellysquid3",
            "No-compromises game logic optimization.",
            "36.9M",
            "Optimization",
            [255, 200, 120],
            [200, 110, 40],
        ),
        (
            "modmenu",
            "Mod Menu",
            "Prospector",
            "Adds a menu to view installed mods.",
            "54.0M",
            "Utility",
            [110, 190, 255],
            [40, 90, 200],
        ),
        (
            "fabric-api",
            "Fabric API",
            "modmuss50",
            "Lightweight and modular API providing common hooks.",
            "110M",
            "Library",
            [220, 200, 160],
            [140, 110, 70],
        ),
        (
            "entityculling",
            "Entity Culling",
            "tr7zw",
            "Skips rendering entities that are not visible.",
            "30.2M",
            "Optimization",
            [255, 140, 170],
            [190, 50, 100],
        ),
    ];
    let items: Vec<ProjectItem> = projects
        .iter()
        .enumerate()
        .map(|(i, (slug, t, a, d, dl, c, c1, c2))| ProjectItem {
            id: i.to_string().into(),
            title: (*t).into(),
            author: (*a).into(),
            description: (*d).into(),
            downloads: (*dl).into(),
            categories: (*c).into(),
            icon: project_icon(slug, *c1, *c2),
            has_icon: true,
            installed: i == 4,
            installing: false,
        })
        .collect();
    app.set_projects(ModelRc::new(VecModel::from(items)));
    app.set_projects_total("41,203 results".into());
    app.set_browse_categories(ModelRc::new(VecModel::from(vec!["All categories".into()])));

    let content: Vec<ContentItem> = projects
        .iter()
        .take(4)
        .enumerate()
        .map(|(i, (slug, t, _, _, _, _, c1, c2))| ContentItem {
            file_name: format!("{i}.jar").into(),
            title: (*t).into(),
            subtitle: format!("0.{}.{} · {}.jar", 5 + i, i, t.to_lowercase().replace(' ', "-")).into(),
            icon: project_icon(slug, *c1, *c2),
            has_icon: true,
            enabled: i != 2,
            has_update: i == 1,
            updating: false,
        })
        .collect();
    app.set_content(ModelRc::new(VecModel::from(content)));
    app.set_updates_available(1);

    let logs: Vec<LogItem> = [
        (1, "[Render thread/Info] [Minecraft] Backend library: LWJGL version 3.3.3"),
        (1, "[Render thread/Info] [Minecraft] Reloading ResourceManager: vanilla, fabric, sodium"),
        (2, "[Worker-Main-4/Warn] [ModelBakery] Missing model for fabric:block/example"),
        (1, "[Render thread/Info] [SoundEngine] Sound engine started"),
        (1, "[Render thread/Info] [TextureAtlas] Created: 1024x512x4 minecraft:textures/atlas/blocks.png-atlas"),
        (3, "[Render thread/Error] [GameRenderer] Failed to load shader: post/creeper.json"),
        (1, "[Server thread/Info] [MinecraftServer] Preparing level \"New World\""),
    ]
    .iter()
    .map(|(l, t)| LogItem { level: *l, text: (*t).into() })
    .collect();
    app.set_logs(ModelRc::new(VecModel::from(logs)));
    app.set_default_memory_mb(4096);
    app.set_max_memory_mb(16384);
    app.set_data_dir("/home/you/.local/share/vanta".into());
    app.set_update_status("You're on the latest version.".into());
    app.set_javas(ModelRc::new(VecModel::from(vec![
        JavaItem {
            label: "Java 21 · Vanta".into(),
            path: "~/.local/share/vanta/runtimes/java-runtime-delta/bin/java".into(),
        },
        JavaItem {
            label: "Java 17".into(),
            path: "/usr/lib/jvm/java-17-openjdk/bin/java".into(),
        },
    ])));
}

fn render(window: &MinimalSoftwareWindow, path: &std::path::Path) {
    // Let animations settle.
    for _ in 0..40 {
        CLOCK.with(|c| c.set(c.get() + Duration::from_millis(50)));
        slint::platform::update_timers_and_animations();
        window.request_redraw();
        window.draw_if_needed(|_| {});
    }
    let (w, h) = ((W as f32 * SCALE) as usize, (H as f32 * SCALE) as usize);
    let mut buf = vec![PremultipliedRgbaColor::default(); w * h];
    window.request_redraw();
    window.draw_if_needed(|r| {
        r.render(&mut buf, w);
    });
    let mut img = image::RgbaImage::new(w as u32, h as u32);
    for (p, c) in img.pixels_mut().zip(&buf) {
        *p = image::Rgba([c.red, c.green, c.blue, 255]);
    }
    img.save(path).expect("write png");
    println!("wrote {}", path.display());
}

fn main() {
    let out = std::path::PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| "screenshots".into()));
    std::fs::create_dir_all(&out).unwrap();
    let window = MinimalSoftwareWindow::new(RepaintBufferType::NewBuffer);
    slint::platform::set_platform(Box::new(Headless {
        window: window.clone(),
    }))
    .unwrap();
    window.dispatch_event(WindowEvent::ScaleFactorChanged { scale_factor: SCALE });
    window.set_size(PhysicalSize::new(
        (W as f32 * SCALE) as u32,
        (H as f32 * SCALE) as u32,
    ));

    let ui = AppWindow::new().unwrap();
    fill(&ui);
    ui.show().unwrap();
    let only = std::env::args().nth(2);

    // Opening animation frames (the clock starts at 0 when the window is created).
    if only.as_deref() == Some("splash") {
        let (w, h) = ((W as f32 * SCALE) as usize, (H as f32 * SCALE) as usize);
        for ms in [200u64, 450, 700, 950] {
            while CLOCK.with(|c| c.get()) < Duration::from_millis(ms) {
                CLOCK.with(|c| c.set(c.get() + Duration::from_millis(10)));
                slint::platform::update_timers_and_animations();
            }
            let mut buf = vec![PremultipliedRgbaColor::default(); w * h];
            window.request_redraw();
            window.draw_if_needed(|r| {
                r.render(&mut buf, w);
            });
            let mut img = image::RgbaImage::new(w as u32, h as u32);
            for (p, c) in img.pixels_mut().zip(&buf) {
                *p = image::Rgba([c.red, c.green, c.blue, 255]);
            }
            img.save(out.join(format!("splash-{ms}.png"))).unwrap();
        }
        return;
    }

    let app = ui.global::<App>();
    let versions: Vec<VersionItem> = ["1.21.4", "1.21.1", "1.20.6", "1.20.1", "1.19.4", "1.18.2"]
        .iter()
        .map(|v| VersionItem {
            id: (*v).into(),
            kind: "Release".into(),
            date: "2024-12-03".into(),
        })
        .collect();
    app.set_versions(ModelRc::new(VecModel::from(versions)));
    app.set_loader_versions(ModelRc::new(VecModel::from(vec![slint::SharedString::from(
        "0.16.9",
    )])));
    let shots: [(&str, Page, bool); 6] = [
        ("home", Page::Home, false),
        ("library", Page::Instances, false),
        ("instance", Page::Instance, false),
        ("browse", Page::Browse, false),
        ("settings", Page::Settings, false),
        ("console", Page::Home, true),
    ];
    for (name, page, console) in shots {
        if only.as_deref().is_some_and(|o| o != name) {
            continue;
        }
        app.set_page(page);
        app.set_console_open(console);
        render(&window, &out.join(format!("{name}.png")));
    }
    // Play states on the instance page and home.
    if only.as_deref().is_none_or(|o| o == "launching") {
        app.set_page(Page::Instance);
        app.set_console_open(false);
        app.set_launch_instance_id("survival".into());
        app.set_launch_progress(ProgressInfo {
            active: true,
            stage: "Downloading files".into(),
            fraction: 0.42,
            detail: "312 MB of 733 MB · 41.2 MB/s · 1,804 files left".into(),
        });
        render(&window, &out.join("launching.png"));
        app.set_page(Page::Home);
        render(&window, &out.join("launching-home.png"));
        app.set_launch_progress(ProgressInfo::default());
    }
    if only.as_deref().is_none_or(|o| o == "running") {
        let mut f = app.get_featured();
        f.running = true;
        app.set_featured(f.clone());
        app.set_selected(f);
        app.set_running_time("12:03".into());
        app.set_page(Page::Instance);
        render(&window, &out.join("running.png"));
        let mut f = app.get_featured();
        f.running = false;
        app.set_featured(f.clone());
        app.set_selected(f);
    }
    // Dialogs
    if only.as_deref().is_none_or(|o| o == "create") {
        app.set_page(Page::Home);
        app.set_console_open(false);
        app.set_create_preset_version("1.21.1".into());
        app.set_create_preset_loader(1);
        app.set_create_open(true);
        render(&window, &out.join("create.png"));
        app.set_create_open(false);
    }
    if only.as_deref().is_none_or(|o| o == "project") {
        app.set_page(Page::Browse);
        app.set_console_open(false);
        let first = app.get_projects().row_data(0).unwrap();
        app.set_project(first);
        app.set_project_body("Sodium is a powerful rendering engine for Minecraft which greatly improves frame rates and stuttering while fixing many graphical issues.\n\n• Faster chunk rendering\n• Lower memory use".into());
        app.set_project_open(true);
        render(&window, &out.join("project.png"));
        app.set_project_open(false);
    }
    if only.as_deref().is_none_or(|o| o == "accounts") {
        app.set_page(Page::Home);
        app.set_offline_accounts_enabled(true);
        // What users see today: no Microsoft client ID, no account yet.
        app.set_msa_configured(false);
        app.set_accounts(ModelRc::default());
        app.set_accounts_open(true);
        app.set_toast_text("Created test".into());
        app.set_toast_serial(app.get_toast_serial() + 1);
        render(&window, &out.join("accounts.png"));
        app.set_accounts_open(false);
    }
    let theme = ui.global::<Theme>();
    theme.set_dark(false);
    app.set_page(Page::Home);
    app.set_console_open(false);
    render(&window, &out.join("home-light.png"));
}
