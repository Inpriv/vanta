//! Only one Vanta runs at a time; a second launch asks the first to show
//! its window and exits.

use std::fs::File;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::time::Duration;

pub enum Guard {
    Primary { _lock: File, listener: TcpListener },
    Secondary,
}

const HELLO: &[u8] = b"vanta:show";

pub fn acquire(dir: &Path) -> Guard {
    let _ = std::fs::create_dir_all(dir);
    let lock_path = dir.join("instance.lock");
    let port_path = dir.join("instance.port");
    let Ok(lock) = File::options()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&lock_path)
    else {
        // Can't create a lock file: don't block the user from starting.
        return primary_without_lock(&lock_path, &port_path);
    };
    match lock.try_lock() {
        Ok(()) => {
            let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback");
            let port = listener.local_addr().map(|a| a.port()).unwrap_or(0);
            let _ = std::fs::write(&port_path, port.to_string());
            Guard::Primary {
                _lock: lock,
                listener,
            }
        }
        Err(_) => {
            if let Some(port) = std::fs::read_to_string(&port_path)
                .ok()
                .and_then(|s| s.trim().parse::<u16>().ok())
                && let Ok(mut s) = TcpStream::connect_timeout(
                    &(std::net::Ipv4Addr::LOCALHOST, port).into(),
                    Duration::from_millis(500),
                )
            {
                let _ = s.write_all(HELLO);
            }
            Guard::Secondary
        }
    }
}

fn primary_without_lock(lock_path: &Path, port_path: &Path) -> Guard {
    tracing::warn!(path = %lock_path.display(), "single-instance lock unavailable");
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback");
    if let Ok(addr) = listener.local_addr() {
        let _ = std::fs::write(port_path, addr.port().to_string());
    }
    let lock = tempfile_fallback();
    Guard::Primary {
        _lock: lock,
        listener,
    }
}

fn tempfile_fallback() -> File {
    File::options()
        .create(true)
        .truncate(true)
        .write(true)
        .open(std::env::temp_dir().join("vanta-instance.lock"))
        .expect("temp lock file")
}

/// Call `on_show` whenever another launch asks this instance to come forward.
pub fn listen(listener: TcpListener, on_show: impl Fn() + Send + 'static) {
    std::thread::Builder::new()
        .name("single-instance".into())
        .spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let mut buf = [0u8; 16];
                let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
                if let Ok(n) = stream.read(&mut buf)
                    && &buf[..n] == HELLO
                {
                    on_show();
                }
            }
        })
        .expect("spawn listener thread");
}
