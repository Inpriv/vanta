//! Network integration test: resolves and downloads a real vanilla version.
//!
//! Ignored by default; run with `cargo test -p vanta-core -- --ignored`.
//! Set `VANTA_TEST_VERSION` to pick another version (default `1.20.1`).

use std::time::Instant;

use vanta_core::download::Progress;
use vanta_core::install::Installer;
use vanta_core::launch::{self, LaunchAuth, LaunchOptions};
use vanta_core::launcher::Launcher;
use vanta_core::paths::Paths;

#[tokio::test(flavor = "multi_thread")]
#[ignore = "downloads several hundred MB from Mojang"]
async fn installs_real_vanilla_version() {
    let version_id = std::env::var("VANTA_TEST_VERSION").unwrap_or_else(|_| "1.20.1".into());
    let root = std::env::var("VANTA_TEST_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| tempfile::tempdir().unwrap().keep());
    let launcher = Launcher::new(Paths::new(&root));

    let manifest = launcher.meta.manifest().await.unwrap();
    assert!(manifest.get(&version_id).is_some());

    let version = launcher.meta.resolve(&version_id).await.unwrap();
    let installer = Installer {
        paths: &launcher.paths,
        downloader: &launcher.downloader,
        env: &launcher.env,
    };
    let progress = Progress::new();
    let started = Instant::now();
    let installed = installer.install(&version, &progress).await.unwrap();
    let snap = progress.snapshot();
    eprintln!(
        "installed {version_id}: {} files, {:.1} MB in {:.1}s",
        snap.total_files,
        snap.done_bytes as f64 / 1e6,
        started.elapsed().as_secs_f64()
    );
    assert!(installed.client_jar.is_file());
    for p in &installed.classpath {
        assert!(p.is_file(), "missing {}", p.display());
    }

    // A second run must not download anything.
    let again = Progress::new();
    let t = Instant::now();
    installer.install(&version, &again).await.unwrap();
    eprintln!("verified cached install in {:.2}s", t.elapsed().as_secs_f64());

    let java_req = version.java();
    let java = vanta_core::java::ensure_runtime(
        &launcher.paths,
        &launcher.downloader,
        &java_req.component,
        java_req.major_version,
        &Progress::default(),
    )
    .await
    .unwrap();
    let probed = vanta_core::java::probe(&java).expect("runtime runs");
    assert_eq!(probed.major, java_req.major_version);

    let opts = LaunchOptions {
        java,
        game_dir: root.join("game"),
        assets_dir: launcher.paths.assets_dir(),
        libraries_dir: launcher.paths.libraries_dir(),
        auth: LaunchAuth::dummy("Tester"),
        min_memory_mb: None,
        max_memory_mb: 2048,
        extra_jvm_args: vec![],
        resolution: None,
        launcher_name: "vanta".into(),
        launcher_version: "test".into(),
        client_id: String::new(),
    };
    let cmd = launch::build(&installed, &opts, &launcher.env);
    assert!(cmd.args.contains(&version.main_class));
    assert!(
        !cmd.args.iter().any(|a| a.contains("${")),
        "unresolved placeholder in {:?}",
        cmd.args
    );
}
