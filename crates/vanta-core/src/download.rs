//! Parallel, resumable, verified file downloads.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Duration;

use futures::{StreamExt, TryStreamExt};
use reqwest::StatusCode;
use reqwest::header::RANGE;
use sha1::{Digest, Sha1};
use tokio::fs;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::error::{Error, Result};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DownloadTask {
    pub url: String,
    pub path: PathBuf,
    pub sha1: Option<String>,
    pub size: Option<u64>,
}

impl DownloadTask {
    pub fn new(url: impl Into<String>, path: impl Into<PathBuf>) -> Self {
        Self {
            url: url.into(),
            path: path.into(),
            sha1: None,
            size: None,
        }
    }

    pub fn sha1(mut self, sha1: Option<impl Into<String>>) -> Self {
        self.sha1 = sha1.map(Into::into);
        self
    }

    pub fn size(mut self, size: Option<u64>) -> Self {
        self.size = size;
        self
    }
}

/// How thoroughly already-present files are checked before being skipped.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Verify {
    /// Existing files with the expected size are trusted (fast path).
    #[default]
    Quick,
    /// Every existing file is re-hashed.
    Full,
}

/// Lock-free progress counters, cheap to poll from the UI.
#[derive(Debug, Default)]
pub struct Progress {
    pub total_files: AtomicU64,
    pub done_files: AtomicU64,
    pub total_bytes: AtomicU64,
    pub done_bytes: AtomicU64,
    cancelled: AtomicBool,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ProgressSnapshot {
    pub total_files: u64,
    pub done_files: u64,
    pub total_bytes: u64,
    pub done_bytes: u64,
}

impl ProgressSnapshot {
    pub fn fraction(&self) -> f32 {
        if self.total_bytes > 0 {
            (self.done_bytes as f64 / self.total_bytes as f64).min(1.0) as f32
        } else if self.total_files > 0 {
            self.done_files as f32 / self.total_files as f32
        } else {
            0.0
        }
    }
}

impl Progress {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn snapshot(&self) -> ProgressSnapshot {
        ProgressSnapshot {
            total_files: self.total_files.load(Ordering::Relaxed),
            done_files: self.done_files.load(Ordering::Relaxed),
            total_bytes: self.total_bytes.load(Ordering::Relaxed),
            done_bytes: self.done_bytes.load(Ordering::Relaxed),
        }
    }

    pub fn reset(&self) {
        for c in [
            &self.total_files,
            &self.done_files,
            &self.total_bytes,
            &self.done_bytes,
        ] {
            c.store(0, Ordering::Relaxed);
        }
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Relaxed)
    }
}

#[derive(Clone, Debug)]
pub struct Downloader {
    client: reqwest::Client,
    concurrency: usize,
    retries: u32,
    verify: Verify,
}

impl Downloader {
    pub fn new(client: reqwest::Client) -> Self {
        Self {
            client,
            concurrency: 48,
            retries: 4,
            verify: Verify::Quick,
        }
    }

    pub fn concurrency(mut self, n: usize) -> Self {
        self.concurrency = n.max(1);
        self
    }

    pub fn retries(mut self, n: u32) -> Self {
        self.retries = n;
        self
    }

    pub fn verify(mut self, verify: Verify) -> Self {
        self.verify = verify;
        self
    }

    pub fn client(&self) -> &reqwest::Client {
        &self.client
    }

    /// Download every task that is not already present and valid.
    pub async fn download_all(&self, tasks: Vec<DownloadTask>, progress: &Progress) -> Result<()> {
        let mut seen = HashSet::new();
        let tasks: Vec<_> = tasks
            .into_iter()
            .filter(|t| seen.insert(t.path.clone()))
            .collect();

        progress
            .total_files
            .fetch_add(tasks.len() as u64, Ordering::Relaxed);
        progress
            .total_bytes
            .fetch_add(tasks.iter().filter_map(|t| t.size).sum(), Ordering::Relaxed);

        futures::stream::iter(tasks.into_iter().map(Ok::<_, Error>))
            .try_for_each_concurrent(self.concurrency, |task| async move {
                if progress.is_cancelled() {
                    return Err(Error::Cancelled);
                }
                self.fetch(&task, progress).await?;
                progress.done_files.fetch_add(1, Ordering::Relaxed);
                Ok(())
            })
            .await
    }

    /// Download a single file (skipped if already valid).
    pub async fn fetch(&self, task: &DownloadTask, progress: &Progress) -> Result<()> {
        if self.is_valid(task).await? {
            if let Some(size) = task.size {
                progress.done_bytes.fetch_add(size, Ordering::Relaxed);
            }
            return Ok(());
        }
        if task.url.is_empty() {
            return Err(Error::Status {
                status: 404,
                url: format!("(no download source for {})", task.path.display()),
            });
        }

        let mut attempt = 0;
        loop {
            let counted = AtomicU64::new(0);
            match self.try_fetch(task, progress, &counted).await {
                Ok(()) => {
                    // Keep the byte total honest when the size was unknown or wrong.
                    if let Some(size) = task.size {
                        let got = counted.load(Ordering::Relaxed);
                        if got < size {
                            progress.done_bytes.fetch_add(size - got, Ordering::Relaxed);
                        }
                    }
                    return Ok(());
                }
                Err(e) => {
                    progress
                        .done_bytes
                        .fetch_sub(counted.load(Ordering::Relaxed), Ordering::Relaxed);
                    let retryable = match &e {
                        Error::Status { status, .. } => *status >= 500 || *status == 429,
                        Error::Cancelled => false,
                        _ => true,
                    };
                    if !retryable || attempt >= self.retries {
                        return Err(e);
                    }
                    attempt += 1;
                    let delay = Duration::from_millis(250 * 2u64.pow(attempt));
                    tracing::debug!(url = %task.url, attempt, error = %e, "retrying download");
                    tokio::time::sleep(delay).await;
                }
            }
        }
    }

    async fn is_valid(&self, task: &DownloadTask) -> Result<bool> {
        let Ok(meta) = fs::metadata(&task.path).await else {
            return Ok(false);
        };
        if let Some(size) = task.size
            && meta.len() != size
        {
            return Ok(false);
        }
        match (&task.sha1, self.verify) {
            (Some(expected), Verify::Full) => Ok(sha1_file(&task.path).await?.eq_ignore_ascii_case(expected)),
            // Without a size we can only trust the hash.
            (Some(expected), Verify::Quick) if task.size.is_none() => {
                Ok(sha1_file(&task.path).await?.eq_ignore_ascii_case(expected))
            }
            _ => Ok(true),
        }
    }

    async fn try_fetch(&self, task: &DownloadTask, progress: &Progress, counted: &AtomicU64) -> Result<()> {
        let part = part_path(&task.path);
        if let Some(parent) = task.path.parent() {
            fs::create_dir_all(parent).await.map_err(Error::io_at(parent))?;
        }

        let existing = fs::metadata(&part).await.map(|m| m.len()).unwrap_or(0);
        let mut request = self.client.get(&task.url);
        if existing > 0 {
            request = request.header(RANGE, format!("bytes={existing}-"));
        }
        let response = request.send().await?;
        let status = response.status();

        let mut hasher = Sha1::new();
        let mut file = if status == StatusCode::PARTIAL_CONTENT && existing > 0 {
            hash_into(&part, &mut hasher).await?;
            progress.done_bytes.fetch_add(existing, Ordering::Relaxed);
            counted.fetch_add(existing, Ordering::Relaxed);
            fs::OpenOptions::new()
                .append(true)
                .open(&part)
                .await
                .map_err(Error::io_at(&part))?
        } else if status.is_success() {
            fs::File::create(&part).await.map_err(Error::io_at(&part))?
        } else {
            if status == StatusCode::RANGE_NOT_SATISFIABLE {
                let _ = fs::remove_file(&part).await;
            }
            return Err(Error::Status {
                status: status.as_u16(),
                url: task.url.clone(),
            });
        };

        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            if progress.is_cancelled() {
                return Err(Error::Cancelled);
            }
            let chunk = chunk?;
            hasher.update(&chunk);
            file.write_all(&chunk).await.map_err(Error::io_at(&part))?;
            let n = chunk.len() as u64;
            progress.done_bytes.fetch_add(n, Ordering::Relaxed);
            counted.fetch_add(n, Ordering::Relaxed);
        }
        file.flush().await?;
        drop(file);

        if let Some(expected) = &task.sha1 {
            let actual = hex::encode(hasher.finalize());
            if !actual.eq_ignore_ascii_case(expected) {
                let _ = fs::remove_file(&part).await;
                return Err(Error::HashMismatch {
                    path: task.path.clone(),
                    expected: expected.clone(),
                    actual,
                });
            }
        }
        fs::rename(&part, &task.path)
            .await
            .map_err(Error::io_at(&task.path))?;
        Ok(())
    }
}

fn part_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".part");
    path.with_file_name(name)
}

async fn hash_into(path: &Path, hasher: &mut Sha1) -> Result<()> {
    let mut file = fs::File::open(path).await.map_err(Error::io_at(path))?;
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).await?;
        if n == 0 {
            return Ok(());
        }
        hasher.update(&buf[..n]);
    }
}

/// Hex SHA-1 of a file.
pub async fn sha1_file(path: &Path) -> Result<String> {
    let mut hasher = Sha1::new();
    hash_into(path, &mut hasher).await?;
    Ok(hex::encode(hasher.finalize()))
}

/// Hex SHA-1 of a file (blocking variant for use inside `spawn_blocking`).
pub fn sha1_file_blocking(path: &Path) -> Result<String> {
    use std::io::Read;
    let mut file = std::fs::File::open(path).map_err(Error::io_at(path))?;
    let mut hasher = Sha1::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

pub fn sha1_hex(data: &[u8]) -> String {
    hex::encode(Sha1::digest(data))
}

pub fn sha512_hex(data: &[u8]) -> String {
    use sha2::Sha512;
    hex::encode(Sha512::digest(data))
}

/// Fetch a small JSON document, caching it on disk and falling back to the
/// cached copy when offline.
pub async fn fetch_json_cached<T: serde::de::DeserializeOwned>(
    client: &reqwest::Client,
    url: &str,
    cache: &Path,
) -> Result<T> {
    let fresh = async {
        let response = client.get(url).send().await?;
        if !response.status().is_success() {
            return Err(Error::Status {
                status: response.status().as_u16(),
                url: url.to_owned(),
            });
        }
        let bytes = response.bytes().await?;
        let value: T = serde_json::from_slice(&bytes)?;
        if let Some(parent) = cache.parent() {
            fs::create_dir_all(parent).await?;
        }
        let tmp = part_path(cache);
        fs::write(&tmp, &bytes).await?;
        fs::rename(&tmp, cache).await?;
        Ok(value)
    };
    match fresh.await {
        Ok(v) => Ok(v),
        Err(e) => match fs::read(cache).await {
            Ok(bytes) => {
                tracing::warn!(%url, error = %e, "using cached copy");
                Ok(serde_json::from_slice(&bytes)?)
            }
            Err(_) => Err(e),
        },
    }
}

/// Fetch JSON without caching.
pub async fn fetch_json<T: serde::de::DeserializeOwned>(client: &reqwest::Client, url: &str) -> Result<T> {
    let response = client.get(url).send().await?;
    if !response.status().is_success() {
        return Err(Error::Status {
            status: response.status().as_u16(),
            url: url.to_owned(),
        });
    }
    Ok(response.json().await?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn downloader() -> Downloader {
        Downloader::new(reqwest::Client::new()).retries(2)
    }

    #[tokio::test]
    async fn downloads_and_verifies() {
        let server = MockServer::start().await;
        let body = b"hello vanta".to_vec();
        Mock::given(method("GET"))
            .and(path("/a.bin"))
            .respond_with(ResponseTemplate::new(200).set_body_bytes(body.clone()))
            .mount(&server)
            .await;

        let dir = tempfile::tempdir().unwrap();
        let task = DownloadTask::new(format!("{}/a.bin", server.uri()), dir.path().join("x/a.bin"))
            .sha1(Some(sha1_hex(&body)))
            .size(Some(body.len() as u64));
        let progress = Progress::new();
        downloader()
            .download_all(vec![task.clone(), task.clone()], &progress)
            .await
            .unwrap();
        assert_eq!(std::fs::read(&task.path).unwrap(), body);
        let snap = progress.snapshot();
        assert_eq!(snap.total_files, 1, "duplicate tasks are merged");
        assert_eq!(snap.done_files, 1);
        assert_eq!(snap.done_bytes, body.len() as u64);
    }

    #[tokio::test]
    async fn rejects_bad_hash() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_bytes(b"tampered".to_vec()))
            .mount(&server)
            .await;
        let dir = tempfile::tempdir().unwrap();
        let task = DownloadTask::new(server.uri(), dir.path().join("f")).sha1(Some(sha1_hex(b"x")));
        let err = downloader().fetch(&task, &Progress::default()).await.unwrap_err();
        assert!(matches!(err, Error::HashMismatch { .. }));
        assert!(!task.path.exists());
    }

    #[tokio::test]
    async fn retries_server_errors() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(503))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_bytes(b"ok".to_vec()))
            .mount(&server)
            .await;
        let dir = tempfile::tempdir().unwrap();
        let task = DownloadTask::new(server.uri(), dir.path().join("f")).sha1(Some(sha1_hex(b"ok")));
        downloader().fetch(&task, &Progress::default()).await.unwrap();
        assert_eq!(std::fs::read(&task.path).unwrap(), b"ok");
    }

    #[tokio::test]
    async fn does_not_retry_not_found() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(404))
            .expect(1)
            .mount(&server)
            .await;
        let dir = tempfile::tempdir().unwrap();
        let task = DownloadTask::new(server.uri(), dir.path().join("f"));
        let err = downloader().fetch(&task, &Progress::default()).await.unwrap_err();
        assert!(matches!(err, Error::Status { status: 404, .. }));
    }

    #[tokio::test]
    async fn resumes_partial_download() {
        let server = MockServer::start().await;
        let full = b"0123456789".to_vec();
        Mock::given(method("GET"))
            .and(header("range", "bytes=4-"))
            .respond_with(ResponseTemplate::new(206).set_body_bytes(full[4..].to_vec()))
            .expect(1)
            .mount(&server)
            .await;

        let dir = tempfile::tempdir().unwrap();
        let dest = dir.path().join("big.bin");
        std::fs::write(part_path(&dest), &full[..4]).unwrap();
        let task = DownloadTask::new(server.uri(), &dest).sha1(Some(sha1_hex(&full)));
        downloader().fetch(&task, &Progress::default()).await.unwrap();
        assert_eq!(std::fs::read(&dest).unwrap(), full);
    }

    #[tokio::test]
    async fn skips_existing_valid_file() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(500))
            .expect(0)
            .mount(&server)
            .await;
        let dir = tempfile::tempdir().unwrap();
        let dest = dir.path().join("f");
        std::fs::write(&dest, b"cached").unwrap();
        let task = DownloadTask::new(server.uri(), &dest)
            .sha1(Some(sha1_hex(b"cached")))
            .size(Some(6));
        downloader()
            .verify(Verify::Full)
            .fetch(&task, &Progress::default())
            .await
            .unwrap();
    }
}
