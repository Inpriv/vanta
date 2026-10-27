//! OS keychain backed secret store (Windows Credential Manager, macOS
//! Keychain, Secret Service on Linux).

use vanta_core::accounts::SecretStore;

const SERVICE: &str = "xyz.getvanta.Vanta";
/// Windows limits a credential blob to 2560 bytes (UTF-16), so long values
/// are split across several entries.
const CHUNK: usize = 1000;

pub struct Keychain;

fn entry(key: &str) -> keyring::Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, key)
}

impl SecretStore for Keychain {
    fn get(&self, key: &str) -> Option<String> {
        let head = entry(key).ok()?.get_password().ok()?;
        let Some(count) = head.strip_prefix("chunks:") else {
            return Some(head);
        };
        let count: usize = count.parse().ok()?;
        let mut out = String::new();
        for i in 0..count {
            out.push_str(&entry(&format!("{key}#{i}")).ok()?.get_password().ok()?);
        }
        Some(out)
    }

    fn set(&self, key: &str, value: &str) -> Result<(), String> {
        self.delete(key);
        let chars: Vec<char> = value.chars().collect();
        let chunks: Vec<String> = chars.chunks(CHUNK).map(|c| c.iter().collect()).collect();
        let err = |e: keyring::Error| format!("could not store credentials in the system keychain: {e}");
        for (i, chunk) in chunks.iter().enumerate() {
            entry(&format!("{key}#{i}"))
                .and_then(|e| e.set_password(chunk))
                .map_err(err)?;
        }
        entry(key)
            .and_then(|e| e.set_password(&format!("chunks:{}", chunks.len())))
            .map_err(err)
    }

    fn delete(&self, key: &str) {
        if let Ok(head) = entry(key).and_then(|e| e.get_password())
            && let Some(count) = head.strip_prefix("chunks:").and_then(|c| c.parse::<usize>().ok())
        {
            for i in 0..count {
                if let Ok(e) = entry(&format!("{key}#{i}")) {
                    let _ = e.delete_credential();
                }
            }
        }
        if let Ok(e) = entry(key) {
            let _ = e.delete_credential();
        }
    }
}
