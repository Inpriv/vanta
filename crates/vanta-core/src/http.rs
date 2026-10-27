use std::time::Duration;

/// `User-Agent` sent with every request (Modrinth and Mojang ask for one that
/// identifies the application).
pub const USER_AGENT: &str = concat!(
    "Inpriv/vanta/",
    env!("CARGO_PKG_VERSION"),
    " (+https://github.com/Inpriv/vanta)"
);

/// Install rustls' `ring` crypto provider (smaller than the aws-lc default).
/// Safe to call any number of times.
pub fn install_crypto_provider() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}

/// Shared HTTP client: rustls, HTTP/2, connection pooling.
pub fn client() -> reqwest::Client {
    install_crypto_provider();
    reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .connect_timeout(Duration::from_secs(10))
        .read_timeout(Duration::from_secs(30))
        .pool_max_idle_per_host(64)
        .build()
        .expect("failed to build HTTP client")
}
