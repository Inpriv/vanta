//! Java detection and managed runtime downloads.

use std::collections::{BTreeSet, HashMap};
use std::io::Read;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::download::{self, DownloadTask, Downloader, Progress};
use crate::error::{Error, Result};
use crate::paths::Paths;

pub const MOJANG_RUNTIMES_URL: &str = "https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json";
pub const ADOPTIUM_API: &str = "https://api.adoptium.net/v3";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct JavaInstall {
    /// Path to the `java` (or `javaw.exe`) executable.
    pub path: PathBuf,
    pub version: String,
    pub major: u32,
    pub arch: String,
    /// Installed and managed by Vanta.
    #[serde(default)]
    pub managed: bool,
}

/// `1.8.0_392` → 8, `17.0.9` → 17, `21` → 21.
pub fn parse_major(version: &str) -> Option<u32> {
    let mut parts = version.trim().trim_matches('"').split(['.', '_', '-', '+']);
    let first: u32 = parts.next()?.parse().ok()?;
    if first == 1 {
        parts.next()?.parse().ok()
    } else {
        Some(first)
    }
}

fn exe_name() -> &'static str {
    if cfg!(windows) { "javaw.exe" } else { "java" }
}

/// `release` file next to `bin/` → (version, arch).
fn read_release_file(home: &Path) -> Option<(String, String)> {
    let text = std::fs::read_to_string(home.join("release")).ok()?;
    let mut vars = HashMap::new();
    for line in text.lines() {
        if let Some((k, v)) = line.split_once('=') {
            vars.insert(k.trim(), v.trim().trim_matches('"').to_owned());
        }
    }
    Some((
        vars.get("JAVA_VERSION")?.clone(),
        vars.get("OS_ARCH").cloned().unwrap_or_default(),
    ))
}

/// Run `java -XshowSettings:properties -version` and parse the result.
fn probe_by_running(java: &Path) -> Option<(String, String)> {
    let mut cmd = std::process::Command::new(java);
    cmd.args(["-XshowSettings:properties", "-version"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let out = cmd.output().ok()?;
    let text = String::from_utf8_lossy(&out.stderr);
    let prop = |name: &str| {
        text.lines().find_map(|l| {
            l.trim()
                .strip_prefix(name)?
                .trim()
                .strip_prefix('=')
                .map(|v| v.trim().to_owned())
        })
    };
    Some((prop("java.version")?, prop("os.arch").unwrap_or_default()))
}

pub fn probe(java: &Path) -> Option<JavaInstall> {
    let java = java.canonicalize().ok()?;
    let home = java.parent()?.parent()?;
    let (version, arch) = read_release_file(home).or_else(|| probe_by_running(&java))?;
    Some(JavaInstall {
        major: parse_major(&version)?,
        path: java,
        version,
        arch,
        managed: false,
    })
}

fn candidate_homes() -> Vec<PathBuf> {
    let mut homes = Vec::new();
    if let Some(h) = std::env::var_os("JAVA_HOME") {
        homes.push(PathBuf::from(h));
    }
    let mut scan = |dir: PathBuf| {
        if let Ok(rd) = std::fs::read_dir(&dir) {
            for e in rd.flatten() {
                homes.push(e.path());
                // macOS bundles
                homes.push(e.path().join("Contents/Home"));
            }
        }
    };
    if cfg!(windows) {
        for var in ["ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"] {
            if let Some(pf) = std::env::var_os(var) {
                let pf = PathBuf::from(pf);
                for vendor in [
                    "Java",
                    "Eclipse Adoptium",
                    "Eclipse Foundation",
                    "Microsoft",
                    "Zulu",
                    "BellSoft",
                    "Amazon Corretto",
                    "Semeru",
                ] {
                    scan(pf.join(vendor));
                }
            }
        }
    } else {
        for dir in [
            "/usr/lib/jvm",
            "/usr/lib64/jvm",
            "/usr/java",
            "/opt/java",
            "/opt/jdk",
            "/Library/Java/JavaVirtualMachines",
        ] {
            scan(PathBuf::from(dir));
        }
        if let Some(home) = dirs::home_dir() {
            scan(home.join(".sdkman/candidates/java"));
            scan(home.join(".jdks"));
            scan(home.join("Library/Java/JavaVirtualMachines"));
        }
    }
    homes
}

fn on_path() -> Vec<PathBuf> {
    let name = if cfg!(windows) { "java.exe" } else { "java" };
    std::env::var_os("PATH")
        .map(|p| {
            std::env::split_paths(&p)
                .map(|d| d.join(name))
                .filter(|p| p.is_file())
                .collect()
        })
        .unwrap_or_default()
}

/// Find Java installations on this machine (blocking; call from a worker).
pub fn detect(paths: &Paths) -> Vec<JavaInstall> {
    let mut exes: Vec<PathBuf> = candidate_homes()
        .into_iter()
        .map(|h| h.join("bin").join(exe_name()))
        .filter(|p| p.is_file())
        .collect();
    exes.extend(on_path());

    let mut seen = BTreeSet::new();
    let mut found: Vec<JavaInstall> = exes
        .into_iter()
        .filter_map(|p| probe(&p))
        .filter(|j| seen.insert(j.path.clone()))
        .collect();
    found.extend(managed_runtimes(paths));
    found.sort_by(|a, b| b.major.cmp(&a.major).then(a.path.cmp(&b.path)));
    found.dedup_by(|a, b| a.path == b.path);
    found
}

/// Runtimes previously installed by Vanta.
pub fn managed_runtimes(paths: &Paths) -> Vec<JavaInstall> {
    let Ok(rd) = std::fs::read_dir(paths.runtimes_dir()) else {
        return Vec::new();
    };
    rd.flatten()
        .filter_map(|e| {
            let java = find_java_in(&e.path())?;
            let mut j = probe(&java)?;
            j.managed = true;
            Some(j)
        })
        .collect()
}

/// Locate `bin/java` inside an extracted runtime directory.
fn find_java_in(root: &Path) -> Option<PathBuf> {
    if !root.join(".complete").exists() {
        return None;
    }
    for rel in ["bin", "jre.bundle/Contents/Home/bin", "Contents/Home/bin"] {
        let p = root.join(rel).join(exe_name());
        if p.is_file() {
            return Some(p);
        }
    }
    // Adoptium archives contain one top-level directory.
    for e in std::fs::read_dir(root).ok()?.flatten() {
        for rel in ["bin", "Contents/Home/bin"] {
            let p = e.path().join(rel).join(exe_name());
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

/// Pick the best detected Java for a required major version.
pub fn pick(installs: &[JavaInstall], major: u32) -> Option<&JavaInstall> {
    let host = std::env::consts::ARCH;
    let arch_ok = |j: &JavaInstall| {
        j.arch.is_empty()
            || match host {
                "x86_64" => matches!(j.arch.as_str(), "amd64" | "x86_64"),
                "aarch64" => matches!(j.arch.as_str(), "aarch64" | "arm64"),
                _ => true,
            }
    };
    installs
        .iter()
        .filter(|j| j.major == major && arch_ok(j))
        // Prefer Vanta's managed runtimes: known-good and not changed by system updates.
        .max_by_key(|j| j.managed)
}

fn mojang_platform() -> Option<&'static str> {
    Some(match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => "linux",
        ("linux", "x86") => "linux-i386",
        ("windows", "x86_64") => "windows-x64",
        ("windows", "x86") => "windows-x86",
        ("windows", "aarch64") => "windows-arm64",
        ("macos", "x86_64") => "mac-os",
        ("macos", "aarch64") => "mac-os-arm64",
        _ => return None,
    })
}

#[derive(Deserialize)]
struct RuntimeEntry {
    manifest: RuntimeManifestRef,
}

#[derive(Deserialize)]
struct RuntimeManifestRef {
    sha1: String,
    url: String,
}

#[derive(Deserialize)]
struct RuntimeManifest {
    files: HashMap<String, RuntimeFile>,
}

#[derive(Deserialize)]
struct RuntimeFile {
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    executable: bool,
    #[serde(default)]
    downloads: Option<RuntimeDownloads>,
    #[serde(default)]
    target: Option<String>,
}

#[derive(Deserialize)]
struct RuntimeDownloads {
    raw: RuntimeRaw,
}

#[derive(Deserialize)]
struct RuntimeRaw {
    sha1: String,
    size: u64,
    url: String,
}

/// A previously downloaded runtime for `component`/`major`, if any.
pub fn installed_runtime(paths: &Paths, component: &str, major: u32) -> Option<PathBuf> {
    find_java_in(&paths.runtimes_dir().join(component))
        .or_else(|| find_java_in(&paths.runtimes_dir().join(format!("adoptium-{major}"))))
}

/// Ensure a Java runtime for `component`/`major` exists, downloading it if
/// needed. Returns the `java` executable.
pub async fn ensure_runtime(
    paths: &Paths,
    downloader: &Downloader,
    component: &str,
    major: u32,
    progress: &Progress,
) -> Result<PathBuf> {
    let mojang_dir = paths.runtimes_dir().join(component);
    if let Some(java) = find_java_in(&mojang_dir) {
        return Ok(java);
    }
    let adoptium_dir = paths.runtimes_dir().join(format!("adoptium-{major}"));
    if let Some(java) = find_java_in(&adoptium_dir) {
        return Ok(java);
    }

    match install_mojang_runtime(paths, downloader, component, progress).await {
        Ok(Some(java)) => return Ok(java),
        Ok(None) => tracing::info!(component, "no Mojang runtime for this platform, using Adoptium"),
        Err(e) => tracing::warn!(component, error = %e, "Mojang runtime failed, using Adoptium"),
    }
    install_adoptium(downloader, &adoptium_dir, major, progress).await
}

async fn install_mojang_runtime(
    paths: &Paths,
    downloader: &Downloader,
    component: &str,
    progress: &Progress,
) -> Result<Option<PathBuf>> {
    let Some(platform) = mojang_platform() else {
        return Ok(None);
    };
    let client = downloader.client();
    let all: HashMap<String, HashMap<String, Vec<RuntimeEntry>>> = download::fetch_json_cached(
        client,
        MOJANG_RUNTIMES_URL,
        &paths.runtimes_dir().join("all.json"),
    )
    .await?;
    let Some(entry) = all
        .get(platform)
        .and_then(|p| p.get(component))
        .and_then(|v| v.first())
    else {
        return Ok(None);
    };

    let dir = paths.runtimes_dir().join(component);
    let manifest_path = paths.runtimes_dir().join(format!("{component}.manifest.json"));
    downloader
        .fetch(
            &DownloadTask::new(&entry.manifest.url, &manifest_path).sha1(Some(entry.manifest.sha1.clone())),
            &Progress::default(),
        )
        .await?;
    let manifest: RuntimeManifest = serde_json::from_slice(&tokio::fs::read(&manifest_path).await?)?;

    let mut tasks = Vec::new();
    let mut executables = Vec::new();
    let mut links = Vec::new();
    for (rel, file) in &manifest.files {
        let path = dir.join(rel);
        match file.kind.as_str() {
            "directory" => tokio::fs::create_dir_all(&path).await?,
            "file" => {
                if let Some(d) = &file.downloads {
                    tasks.push(
                        DownloadTask::new(&d.raw.url, &path)
                            .sha1(Some(d.raw.sha1.clone()))
                            .size(Some(d.raw.size)),
                    );
                    if file.executable {
                        executables.push(path);
                    }
                }
            }
            "link" => {
                if let Some(target) = &file.target {
                    links.push((path, PathBuf::from(target)));
                }
            }
            _ => {}
        }
    }
    downloader.download_all(tasks, progress).await?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for exe in &executables {
            let _ = std::fs::set_permissions(exe, std::fs::Permissions::from_mode(0o755));
        }
        for (link, target) in &links {
            let _ = std::fs::remove_file(link);
            if let Some(parent) = link.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let _ = std::os::unix::fs::symlink(target, link);
        }
    }
    #[cfg(not(unix))]
    let _ = (&executables, &links);

    tokio::fs::write(dir.join(".complete"), b"").await?;
    find_java_in(&dir)
        .map(Some)
        .ok_or_else(|| Error::Java(format!("runtime {component} has no java executable")))
}

#[derive(Deserialize)]
struct AdoptiumAsset {
    binary: AdoptiumBinary,
}

#[derive(Deserialize)]
struct AdoptiumBinary {
    package: AdoptiumPackage,
}

#[derive(Deserialize)]
struct AdoptiumPackage {
    checksum: String,
    link: String,
    name: String,
    size: u64,
}

async fn install_adoptium(
    downloader: &Downloader,
    dir: &Path,
    major: u32,
    progress: &Progress,
) -> Result<PathBuf> {
    let os = match std::env::consts::OS {
        "windows" => "windows",
        "macos" => "mac",
        _ => "linux",
    };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "aarch64",
        "x86" => "x32",
        "arm" => "arm",
        other => other,
    };
    let url = format!(
        "{ADOPTIUM_API}/assets/latest/{major}/hotspot?architecture={arch}&image_type=jre&os={os}&vendor=eclipse"
    );
    let assets: Vec<AdoptiumAsset> = download::fetch_json(downloader.client(), &url).await?;
    let pkg = &assets
        .first()
        .ok_or_else(|| Error::Java(format!("no Java {major} build available for {os}/{arch}")))?
        .binary
        .package;

    let archive = dir.with_extension("download");
    // Adoptium only publishes SHA-256, so verify it here rather than in the downloader.
    downloader
        .fetch(
            &DownloadTask::new(&pkg.link, &archive).size(Some(pkg.size)),
            progress,
        )
        .await?;
    let bytes = tokio::fs::read(&archive).await?;
    let actual = {
        use sha2::{Digest, Sha256};
        hex::encode(Sha256::digest(&bytes))
    };
    if !actual.eq_ignore_ascii_case(&pkg.checksum) {
        let _ = tokio::fs::remove_file(&archive).await;
        return Err(Error::HashMismatch {
            path: archive,
            expected: pkg.checksum.clone(),
            actual,
        });
    }

    let dir = dir.to_path_buf();
    let name = pkg.name.clone();
    tokio::task::spawn_blocking(move || -> Result<PathBuf> {
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir)?;
        if name.ends_with(".zip") {
            zip::ZipArchive::new(std::io::Cursor::new(bytes))?.extract(&dir)?;
        } else {
            let gz = flate2::read::GzDecoder::new(std::io::Cursor::new(bytes));
            let mut tar = tar::Archive::new(gz);
            tar.set_preserve_permissions(true);
            tar.unpack(&dir)?;
        }
        std::fs::write(dir.join(".complete"), b"")?;
        let _ = std::fs::remove_file(dir.with_extension("download"));
        find_java_in(&dir).ok_or_else(|| Error::Java("downloaded runtime has no java executable".into()))
    })
    .await
    .map_err(|e| Error::Io(std::io::Error::other(e)))?
}

/// Read `Main-Class` from a jar manifest (used for Forge processors).
pub fn jar_main_class(jar: &Path) -> Result<String> {
    let mut archive = zip::ZipArchive::new(std::fs::File::open(jar).map_err(Error::io_at(jar))?)?;
    let mut manifest = String::new();
    archive
        .by_name("META-INF/MANIFEST.MF")?
        .read_to_string(&mut manifest)?;
    manifest
        .lines()
        .find_map(|l| l.strip_prefix("Main-Class:").map(|v| v.trim().to_owned()))
        .ok_or_else(|| Error::Loader(format!("{} has no Main-Class", jar.display())))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn major_versions() {
        assert_eq!(parse_major("1.8.0_392"), Some(8));
        assert_eq!(parse_major("17.0.9"), Some(17));
        assert_eq!(parse_major("21"), Some(21));
        assert_eq!(parse_major("\"21.0.1+12\""), Some(21));
        assert_eq!(parse_major("garbage"), None);
    }

    #[test]
    fn release_file() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("release"),
            "IMPLEMENTOR=\"Eclipse Adoptium\"\nJAVA_VERSION=\"17.0.9\"\nOS_ARCH=\"x86_64\"\n",
        )
        .unwrap();
        assert_eq!(
            read_release_file(dir.path()),
            Some(("17.0.9".into(), "x86_64".into()))
        );
    }

    #[test]
    fn picks_matching_major_preferring_managed() {
        let j = |major: u32, managed: bool| JavaInstall {
            path: PathBuf::from(format!("/j{major}{managed}")),
            version: major.to_string(),
            major,
            arch: String::new(),
            managed,
        };
        let all = vec![j(8, false), j(17, false), j(17, true), j(21, false)];
        let p = pick(&all, 17).unwrap();
        assert!(p.managed);
        assert!(pick(&all, 11).is_none());
    }
}
