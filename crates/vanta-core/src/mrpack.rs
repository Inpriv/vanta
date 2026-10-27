//! Modrinth modpack format (`.mrpack`) import and export.
//!
//! Spec: <https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack>

use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::content::{Content, ContentKind, ContentMeta};
use crate::download::{self, DownloadTask, Downloader, Progress};
use crate::error::{Error, Result};
use crate::instance::{Instance, InstanceStore, LoaderKind};
use crate::paths::{self, Paths};

pub const INDEX_NAME: &str = "modrinth.index.json";

/// Hosts the spec allows file downloads from.
const ALLOWED_HOSTS: &[&str] = &[
    "cdn.modrinth.com",
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
];

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FileEnv {
    #[serde(default = "required")]
    pub client: String,
    #[serde(default = "required")]
    pub server: String,
}

fn required() -> String {
    "required".into()
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MrpackFile {
    pub path: String,
    pub hashes: HashMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub env: Option<FileEnv>,
    pub downloads: Vec<String>,
    pub file_size: u64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MrpackIndex {
    pub format_version: u32,
    pub game: String,
    pub version_id: String,
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    pub files: Vec<MrpackFile>,
    pub dependencies: HashMap<String, String>,
}

impl MrpackIndex {
    pub fn game_version(&self) -> Result<&str> {
        self.dependencies
            .get("minecraft")
            .map(String::as_str)
            .ok_or_else(|| Error::Modpack("pack does not declare a Minecraft version".into()))
    }

    pub fn loader(&self) -> (LoaderKind, Option<String>) {
        for kind in [
            LoaderKind::Fabric,
            LoaderKind::Quilt,
            LoaderKind::Forge,
            LoaderKind::NeoForge,
        ] {
            if let Some(v) = kind.mrpack_key().and_then(|k| self.dependencies.get(k)) {
                return (kind, Some(v.clone()));
            }
        }
        (LoaderKind::Vanilla, None)
    }
}

/// A relative path that stays inside its root.
pub fn safe_relative(path: &str) -> Result<PathBuf> {
    let p = Path::new(path);
    let bad = || Error::Modpack(format!("unsafe path in pack: `{path}`"));
    if path.is_empty() || path.contains('\\') || path.contains(':') {
        return Err(bad());
    }
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::Normal(s) => out.push(s),
            Component::CurDir => {}
            _ => return Err(bad()),
        }
    }
    if out.as_os_str().is_empty() {
        return Err(bad());
    }
    Ok(out)
}

pub fn allowed_url(url: &str) -> bool {
    url::Url::parse(url)
        .is_ok_and(|u| u.scheme() == "https" && u.host_str().is_some_and(|h| ALLOWED_HOSTS.contains(&h)))
}

pub fn read_index(pack: &Path) -> Result<MrpackIndex> {
    let mut zip = zip::ZipArchive::new(std::fs::File::open(pack).map_err(Error::io_at(pack))?)?;
    let mut s = String::new();
    zip.by_name(INDEX_NAME)
        .map_err(|_| Error::Modpack("not a Modrinth modpack (missing modrinth.index.json)".into()))?
        .read_to_string(&mut s)?;
    let index: MrpackIndex = serde_json::from_str(&s)?;
    if index.game != "minecraft" {
        return Err(Error::Modpack(format!("unsupported game `{}`", index.game)));
    }
    Ok(index)
}

fn kind_for(rel: &Path) -> Option<ContentKind> {
    let first = rel.components().next()?.as_os_str().to_str()?;
    ContentKind::ALL.into_iter().find(|k| k.dir_name() == first)
}

/// Create a new instance from a `.mrpack` file.
pub async fn import(
    paths: &Paths,
    store: &InstanceStore,
    downloader: &Downloader,
    pack: &Path,
    progress: &Progress,
) -> Result<Instance> {
    let index = read_index(pack)?;
    let (loader, loader_version) = index.loader();
    let mut inst = Instance::new(index.name.clone(), index.game_version()?);
    inst.loader = loader;
    inst.loader_version = loader_version;
    let inst = store.create(inst)?;
    let game_dir = store.game_dir(&inst.id);

    let result = async {
        let mut tasks = Vec::new();
        let mut links = Vec::new();
        for f in &index.files {
            if f.env.as_ref().is_some_and(|e| e.client == "unsupported") {
                continue;
            }
            let rel = safe_relative(&f.path)?;
            let sha1 = f
                .hashes
                .get("sha1")
                .ok_or_else(|| Error::Modpack(format!("{} has no sha1 hash", f.path)))?;
            let url =
                f.downloads.iter().find(|u| allowed_url(u)).ok_or_else(|| {
                    Error::Modpack(format!("{} has no download from an allowed host", f.path))
                })?;
            let stored = paths.content_store(sha1);
            tasks.push(
                DownloadTask::new(url, &stored)
                    .sha1(Some(sha1.clone()))
                    .size(Some(f.file_size)),
            );
            links.push((stored, rel, sha1.clone()));
        }
        downloader.download_all(tasks, progress).await?;

        let content = Content {
            instance_dir: &store.dir(&inst.id),
            game_dir: &game_dir,
        };
        for (stored, rel, sha1) in links {
            paths::link_or_copy(&stored, &game_dir.join(&rel))?;
            if let Some(kind) = kind_for(&rel) {
                let name = rel.file_name().unwrap_or_default().to_string_lossy().into_owned();
                // Placeholder metadata; the UI fills in titles via Modrinth.
                content.record(
                    kind,
                    &name,
                    ContentMeta {
                        project_id: String::new(),
                        version_id: String::new(),
                        title: name.clone(),
                        version_number: String::new(),
                        icon_url: None,
                        sha1,
                    },
                )?;
            }
        }

        let pack = pack.to_path_buf();
        let game_dir = game_dir.clone();
        tokio::task::spawn_blocking(move || extract_overrides(&pack, &game_dir))
            .await
            .map_err(|e| Error::Io(std::io::Error::other(e)))??;
        Ok::<_, Error>(())
    }
    .await;

    if let Err(e) = result {
        let _ = store.delete(&inst.id);
        return Err(e);
    }
    Ok(inst)
}

fn extract_overrides(pack: &Path, game_dir: &Path) -> Result<()> {
    let mut zip = zip::ZipArchive::new(std::fs::File::open(pack)?)?;
    // `client-overrides` is applied after `overrides` so it wins.
    for prefix in ["overrides/", "client-overrides/"] {
        for i in 0..zip.len() {
            let mut entry = zip.by_index(i)?;
            let name = entry.name()?.into_owned();
            let Some(rel) = name.strip_prefix(prefix) else {
                continue;
            };
            if rel.is_empty() || entry.is_dir() {
                continue;
            }
            let rel = safe_relative(rel)?;
            let out = game_dir.join(rel);
            if let Some(p) = out.parent() {
                std::fs::create_dir_all(p)?;
            }
            let mut f = std::fs::File::create(&out)?;
            std::io::copy(&mut entry, &mut f)?;
        }
    }
    Ok(())
}

pub struct ExportOptions {
    pub name: String,
    pub version: String,
    pub summary: Option<String>,
    /// Paths relative to the game directory to include (files or folders).
    pub include: Vec<String>,
}

/// Export an instance. `known` maps SHA-1 → download URL for files that
/// exist on Modrinth; those are referenced, everything else becomes an override.
pub fn export(
    inst: &Instance,
    game_dir: &Path,
    opts: &ExportOptions,
    known: &HashMap<String, String>,
    out: &Path,
) -> Result<()> {
    let mut files = Vec::new();
    let mut overrides: Vec<(PathBuf, String)> = Vec::new();

    let mut stack: Vec<PathBuf> = opts
        .include
        .iter()
        .map(|p| safe_relative(p).map(|r| game_dir.join(r)))
        .collect::<Result<_>>()?;
    while let Some(path) = stack.pop() {
        if path.is_dir() {
            for e in std::fs::read_dir(&path)?.flatten() {
                stack.push(e.path());
            }
            continue;
        }
        if !path.is_file() {
            continue;
        }
        let rel = path
            .strip_prefix(game_dir)
            .map_err(|_| Error::Modpack("path outside the instance".into()))?
            .to_string_lossy()
            .replace('\\', "/");
        if rel.ends_with(crate::content::DISABLED_SUFFIX) {
            continue;
        }
        let data = std::fs::read(&path)?;
        let sha1 = download::sha1_hex(&data);
        match known.get(&sha1) {
            Some(url) if kind_for(Path::new(&rel)).is_some() => files.push(MrpackFile {
                path: rel,
                hashes: HashMap::from([
                    ("sha1".into(), sha1),
                    ("sha512".into(), download::sha512_hex(&data)),
                ]),
                env: None,
                downloads: vec![url.clone()],
                file_size: data.len() as u64,
            }),
            _ => overrides.push((path, rel)),
        }
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));

    let mut dependencies = HashMap::from([("minecraft".to_owned(), inst.game_version.clone())]);
    if let (Some(key), Some(v)) = (inst.loader.mrpack_key(), &inst.loader_version) {
        dependencies.insert(key.to_owned(), v.clone());
    }
    let index = MrpackIndex {
        format_version: 1,
        game: "minecraft".into(),
        version_id: opts.version.clone(),
        name: opts.name.clone(),
        summary: opts.summary.clone(),
        files,
        dependencies,
    };

    let tmp = out.with_extension("mrpack.part");
    {
        let mut zip = zip::ZipWriter::new(std::fs::File::create(&tmp).map_err(Error::io_at(&tmp))?);
        let opts =
            zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
        zip.start_file(INDEX_NAME, opts)?;
        zip.write_all(&serde_json::to_vec_pretty(&index)?)?;
        for (path, rel) in overrides {
            zip.start_file(format!("overrides/{rel}"), opts)?;
            zip.write_all(&std::fs::read(path)?)?;
        }
        zip.finish()?;
    }
    std::fs::rename(&tmp, out)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::method;
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[test]
    fn path_safety() {
        assert!(safe_relative("mods/a.jar").is_ok());
        assert!(safe_relative("./config/x.toml").is_ok());
        for bad in ["../evil", "/etc/passwd", "mods/../../x", "C:/x", "a\\b", ""] {
            assert!(safe_relative(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn url_allowlist() {
        assert!(allowed_url("https://cdn.modrinth.com/data/x/y.jar"));
        assert!(allowed_url("https://github.com/a/b/releases/download/x.jar"));
        assert!(!allowed_url("http://cdn.modrinth.com/x.jar"));
        assert!(!allowed_url("https://evil.example/x.jar"));
    }

    #[test]
    fn export_then_read() {
        let dir = tempfile::tempdir().unwrap();
        let game = dir.path().join("g");
        std::fs::create_dir_all(game.join("mods")).unwrap();
        std::fs::create_dir_all(game.join("config")).unwrap();
        std::fs::write(game.join("mods/known.jar"), b"known").unwrap();
        std::fs::write(game.join("mods/custom.jar"), b"custom").unwrap();
        std::fs::write(game.join("mods/off.jar.disabled"), b"off").unwrap();
        std::fs::write(game.join("config/a.toml"), b"x=1").unwrap();

        let mut inst = Instance::new("Pack", "1.20.1");
        inst.loader = LoaderKind::Fabric;
        inst.loader_version = Some("0.15.0".into());
        let known = HashMap::from([(
            download::sha1_hex(b"known"),
            "https://cdn.modrinth.com/k.jar".to_string(),
        )]);
        let out = dir.path().join("pack.mrpack");
        export(
            &inst,
            &game,
            &ExportOptions {
                name: "Pack".into(),
                version: "1.0.0".into(),
                summary: None,
                include: vec!["mods".into(), "config".into()],
            },
            &known,
            &out,
        )
        .unwrap();

        let index = read_index(&out).unwrap();
        assert_eq!(index.files.len(), 1);
        assert_eq!(index.files[0].path, "mods/known.jar");
        assert_eq!(index.loader(), (LoaderKind::Fabric, Some("0.15.0".into())));
        assert_eq!(index.game_version().unwrap(), "1.20.1");

        let mut zip = zip::ZipArchive::new(std::fs::File::open(&out).unwrap()).unwrap();
        assert!(zip.by_name("overrides/mods/custom.jar").is_ok());
        assert!(zip.by_name("overrides/config/a.toml").is_ok());
        assert!(zip.by_name("overrides/mods/off.jar.disabled").is_err());
    }

    fn write_pack(path: &Path, index: &MrpackIndex, overrides: &[(&str, &[u8])]) {
        let mut zip = zip::ZipWriter::new(std::fs::File::create(path).unwrap());
        let o = zip::write::SimpleFileOptions::default();
        zip.start_file(INDEX_NAME, o).unwrap();
        zip.write_all(&serde_json::to_vec(index).unwrap()).unwrap();
        for (name, data) in overrides {
            zip.start_file(*name, o).unwrap();
            zip.write_all(data).unwrap();
        }
        zip.finish().unwrap();
    }

    #[tokio::test]
    async fn import_rejects_disallowed_hosts() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_bytes(b"mod".to_vec()))
            .mount(&server)
            .await;
        let dir = tempfile::tempdir().unwrap();
        let paths = Paths::new(dir.path().join("data"));
        let store = InstanceStore::new(paths.instances_dir());
        let index = MrpackIndex {
            format_version: 1,
            game: "minecraft".into(),
            version_id: "1".into(),
            name: "Test".into(),
            summary: None,
            files: vec![MrpackFile {
                path: "mods/a.jar".into(),
                hashes: HashMap::from([("sha1".into(), download::sha1_hex(b"mod"))]),
                env: None,
                downloads: vec![format!("{}/a.jar", server.uri())],
                file_size: 3,
            }],
            dependencies: HashMap::from([("minecraft".into(), "1.20.1".into())]),
        };
        let pack = dir.path().join("t.mrpack");
        write_pack(&pack, &index, &[("overrides/options.txt", b"fov:1")]);
        let dl = Downloader::new(crate::http::client());
        let err = import(&paths, &store, &dl, &pack, &Progress::default())
            .await
            .unwrap_err();
        assert!(matches!(err, Error::Modpack(_)));
        assert!(store.list().is_empty(), "failed import cleans up");
    }

    #[tokio::test]
    async fn import_overrides_only_pack() {
        let dir = tempfile::tempdir().unwrap();
        let paths = Paths::new(dir.path().join("data"));
        let store = InstanceStore::new(paths.instances_dir());
        let index = MrpackIndex {
            format_version: 1,
            game: "minecraft".into(),
            version_id: "1".into(),
            name: "Overrides".into(),
            summary: None,
            files: vec![],
            dependencies: HashMap::from([
                ("minecraft".into(), "1.20.1".into()),
                ("quilt-loader".into(), "0.26.0".into()),
            ]),
        };
        let pack = dir.path().join("o.mrpack");
        write_pack(
            &pack,
            &index,
            &[
                ("overrides/options.txt", b"fov:1"),
                ("client-overrides/options.txt", b"fov:2"),
                ("overrides/config/x.json", b"{}"),
            ],
        );
        let dl = Downloader::new(crate::http::client());
        let inst = import(&paths, &store, &dl, &pack, &Progress::default())
            .await
            .unwrap();
        assert_eq!(inst.loader, LoaderKind::Quilt);
        let game = store.game_dir(&inst.id);
        assert_eq!(
            std::fs::read_to_string(game.join("options.txt")).unwrap(),
            "fov:2"
        );
        assert!(game.join("config/x.json").exists());
    }
}
