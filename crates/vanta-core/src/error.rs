use std::path::PathBuf;

/// Errors produced by `vanta-core`.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("network error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("I/O error at {path}: {source}")]
    IoAt {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("invalid JSON: {0}")]
    Json(#[from] serde_json::Error),

    #[error("archive error: {0}")]
    Zip(#[from] zip::result::ZipError),

    #[error("server returned HTTP {status} for {url}")]
    Status { status: u16, url: String },

    #[error("checksum mismatch for {path}: expected {expected}, got {actual}")]
    HashMismatch {
        path: PathBuf,
        expected: String,
        actual: String,
    },

    #[error("version `{0}` was not found")]
    VersionNotFound(String),

    #[error("invalid Maven coordinate `{0}`")]
    InvalidCoordinate(String),

    #[error("authentication failed: {0}")]
    Auth(#[from] crate::auth::AuthError),

    #[error("Java: {0}")]
    Java(String),

    #[error("mod loader: {0}")]
    Loader(String),

    #[error("modpack: {0}")]
    Modpack(String),

    #[error("instance: {0}")]
    Instance(String),

    #[error("operation cancelled")]
    Cancelled,
}

impl Error {
    pub(crate) fn io_at(path: impl Into<PathBuf>) -> impl FnOnce(std::io::Error) -> Error {
        let path = path.into();
        move |source| Error::IoAt { path, source }
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
