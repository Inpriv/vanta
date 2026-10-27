//! Network integration tests for mod loader installation. Ignored by default.

use vanta_core::download::Progress;
use vanta_core::instance::{Instance, LoaderKind};
use vanta_core::launch::{self, LaunchAuth, LaunchOptions};
use vanta_core::launcher::Launcher;
use vanta_core::paths::Paths;

fn launcher() -> Launcher {
    let root = std::env::var("VANTA_TEST_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| tempfile::tempdir().unwrap().keep());
    Launcher::new(Paths::new(root))
}

async fn check(kind: LoaderKind, game_version: &str) {
    let l = launcher();
    let mut inst = Instance::new(format!("{kind:?} {game_version}"), game_version);
    inst.loader = kind;
    let mut inst = l.instances.create(inst).unwrap();
    let progress = Progress::new();
    let installed = l
        .install(&mut inst, &progress, &|s| {
            eprintln!("{kind:?} {game_version}: {}", s.label())
        })
        .await
        .unwrap_or_else(|e| panic!("{kind:?} {game_version}: {e}"));
    for p in &installed.classpath {
        assert!(p.is_file(), "missing {}", p.display());
    }
    assert!(inst.installed_version_id.is_some());

    let opts = LaunchOptions {
        java: "java".into(),
        game_dir: l.instances.game_dir(&inst.id),
        assets_dir: l.paths.assets_dir(),
        libraries_dir: l.paths.libraries_dir(),
        auth: LaunchAuth::dummy("Tester"),
        min_memory_mb: None,
        max_memory_mb: 2048,
        extra_jvm_args: vec![],
        resolution: None,
        launcher_name: "vanta".into(),
        launcher_version: "test".into(),
        client_id: String::new(),
    };
    let cmd = launch::build(&installed, &opts, &l.env);
    assert!(
        !cmd.args.iter().any(|a| a.contains("${")),
        "unresolved placeholder: {:?}",
        cmd.args
    );
    eprintln!(
        "{kind:?} {game_version}: ok, main class {}",
        installed.version.main_class
    );
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "network"]
async fn fabric() {
    check(LoaderKind::Fabric, "1.20.1").await;
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "network"]
async fn quilt() {
    check(LoaderKind::Quilt, "1.20.1").await;
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "network; runs installer processors"]
async fn forge_modern() {
    check(LoaderKind::Forge, "1.20.1").await;
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "network"]
async fn forge_legacy() {
    check(LoaderKind::Forge, "1.12.2").await;
}

#[tokio::test(flavor = "multi_thread")]
#[ignore = "network; runs installer processors"]
async fn neoforge() {
    check(LoaderKind::NeoForge, "1.21.1").await;
}
