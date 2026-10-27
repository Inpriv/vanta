//! Account list and session management.
//!
//! Only non-secret metadata (name, UUID, skin URL) is written to
//! `accounts.json`. Tokens go through a [`SecretStore`], which the app backs
//! with the operating system keychain.

use std::path::PathBuf;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::auth::{AuthClient, AuthError, MinecraftSession, MsaTokens};
use crate::error::Result;
use crate::launch::LaunchAuth;

/// Storage for secrets. Implementations must never log values.
pub trait SecretStore: Send + Sync {
    fn get(&self, key: &str) -> Option<String>;
    fn set(&self, key: &str, value: &str) -> std::result::Result<(), String>;
    fn delete(&self, key: &str);
}

/// In-memory store for tests.
#[derive(Default)]
pub struct MemoryStore(std::sync::Mutex<std::collections::HashMap<String, String>>);

impl SecretStore for MemoryStore {
    fn get(&self, key: &str) -> Option<String> {
        self.0.lock().unwrap().get(key).cloned()
    }
    fn set(&self, key: &str, value: &str) -> std::result::Result<(), String> {
        self.0.lock().unwrap().insert(key.into(), value.into());
        Ok(())
    }
    fn delete(&self, key: &str) {
        self.0.lock().unwrap().remove(key);
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Account {
    /// Profile UUID without dashes.
    pub uuid: String,
    pub name: String,
    #[serde(default)]
    pub skin_url: Option<String>,
    /// Ownership was verified against Minecraft Services at least once.
    #[serde(default)]
    pub verified: bool,
    pub added: DateTime<Utc>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct AccountList {
    #[serde(default)]
    pub active: Option<String>,
    #[serde(default)]
    pub accounts: Vec<Account>,
}

#[derive(Clone, Serialize, Deserialize)]
struct Secrets {
    msa: MsaTokens,
    mc_token: String,
    mc_expires_at: DateTime<Utc>,
}

impl std::fmt::Debug for Secrets {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Secrets(<redacted>)")
    }
}

/// How a launch will authenticate.
#[derive(Clone, Debug)]
pub enum SessionKind {
    Online,
    /// Token refresh failed (e.g. no network) but the account verified
    /// ownership before; the game runs without online features.
    Offline,
}

pub struct AccountManager {
    file: PathBuf,
    secrets: Arc<dyn SecretStore>,
    auth: AuthClient,
    pub list: AccountList,
}

fn secret_key(uuid: &str) -> String {
    format!("account:{uuid}")
}

impl AccountManager {
    pub fn load(file: PathBuf, secrets: Arc<dyn SecretStore>, auth: AuthClient) -> Self {
        let list = std::fs::read(&file)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        Self {
            file,
            secrets,
            auth,
            list,
        }
    }

    pub fn auth(&self) -> &AuthClient {
        &self.auth
    }

    pub fn save(&self) -> Result<()> {
        if let Some(parent) = self.file.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let tmp = self.file.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(&self.list)?)?;
        std::fs::rename(&tmp, &self.file)?;
        Ok(())
    }

    pub fn active(&self) -> Option<&Account> {
        let id = self.list.active.as_ref()?;
        self.list.accounts.iter().find(|a| &a.uuid == id)
    }

    pub fn set_active(&mut self, uuid: &str) -> Result<()> {
        if self.list.accounts.iter().any(|a| a.uuid == uuid) {
            self.list.active = Some(uuid.to_owned());
            self.save()?;
        }
        Ok(())
    }

    /// Store a freshly signed-in account and make it active.
    pub fn add(&mut self, tokens: MsaTokens, session: MinecraftSession) -> Result<Account> {
        let account = Account {
            uuid: session.profile.id.clone(),
            name: session.profile.name.clone(),
            skin_url: session.profile.active_skin().map(|s| s.url.clone()),
            verified: true,
            added: Utc::now(),
        };
        self.store_secrets(&account.uuid, &tokens, &session)?;
        self.list.accounts.retain(|a| a.uuid != account.uuid);
        self.list.accounts.push(account.clone());
        self.list.active = Some(account.uuid.clone());
        self.save()?;
        Ok(account)
    }

    pub fn remove(&mut self, uuid: &str) -> Result<()> {
        self.secrets.delete(&secret_key(uuid));
        self.list.accounts.retain(|a| a.uuid != uuid);
        if self.list.active.as_deref() == Some(uuid) {
            self.list.active = self.list.accounts.first().map(|a| a.uuid.clone());
        }
        self.save()
    }

    fn store_secrets(&self, uuid: &str, msa: &MsaTokens, session: &MinecraftSession) -> Result<()> {
        let secrets = Secrets {
            msa: msa.clone(),
            mc_token: session.access_token.clone(),
            mc_expires_at: session.expires_at,
        };
        self.secrets
            .set(&secret_key(uuid), &serde_json::to_string(&secrets)?)
            .map_err(|e| {
                crate::Error::Auth(AuthError::Unexpected {
                    service: "keychain",
                    detail: e,
                })
            })
    }

    fn load_secrets(&self, uuid: &str) -> Option<Secrets> {
        serde_json::from_str(&self.secrets.get(&secret_key(uuid))?).ok()
    }

    /// Credentials for launching with `uuid`, refreshing tokens if needed.
    pub async fn session(&mut self, uuid: &str) -> Result<(LaunchAuth, SessionKind)> {
        let account = self
            .list
            .accounts
            .iter()
            .find(|a| a.uuid == uuid)
            .cloned()
            .ok_or(AuthError::SessionExpired)?;
        let secrets = self.load_secrets(uuid).ok_or(AuthError::SessionExpired)?;

        let launch = |token: String| LaunchAuth {
            player_name: account.name.clone(),
            uuid: account.uuid.clone(),
            access_token: token,
            xuid: String::new(),
            user_type: "msa".into(),
        };

        if secrets.mc_expires_at > Utc::now() + chrono::Duration::minutes(10) {
            return Ok((launch(secrets.mc_token), SessionKind::Online));
        }

        let refreshed = async {
            let msa = self.auth.refresh(&secrets.msa.refresh_token).await?;
            let session = self.auth.login_minecraft(&msa.access_token).await?;
            Ok::<_, AuthError>((msa, session))
        }
        .await;

        match refreshed {
            Ok((msa, session)) => {
                self.store_secrets(uuid, &msa, &session)?;
                if let Some(a) = self.list.accounts.iter_mut().find(|a| a.uuid == uuid) {
                    a.name = session.profile.name.clone();
                    a.skin_url = session.profile.active_skin().map(|s| s.url.clone());
                    a.verified = true;
                }
                self.save()?;
                Ok((launch(session.access_token), SessionKind::Online))
            }
            Err(AuthError::Network(e)) if account.verified => {
                tracing::warn!(error = %e, "token refresh failed, launching offline");
                Ok((launch("0".into()), SessionKind::Offline))
            }
            Err(e) => Err(e.into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::Profile;

    fn session(id: &str, expires_in_minutes: i64) -> MinecraftSession {
        MinecraftSession {
            access_token: format!("mc-{id}"),
            expires_at: Utc::now() + chrono::Duration::minutes(expires_in_minutes),
            profile: Profile {
                id: id.into(),
                name: format!("player-{id}"),
                skins: vec![],
            },
            xuid: String::new(),
        }
    }

    fn tokens() -> MsaTokens {
        MsaTokens {
            access_token: "a".into(),
            refresh_token: "r".into(),
            expires_at: Utc::now(),
        }
    }

    #[tokio::test]
    async fn add_switch_remove() {
        let dir = tempfile::tempdir().unwrap();
        let store = Arc::new(MemoryStore::default());
        let file = dir.path().join("accounts.json");
        let auth = AuthClient::new(reqwest::Client::new(), "cid");
        let mut m = AccountManager::load(file.clone(), store.clone(), auth.clone());
        m.add(tokens(), session("a", 60)).unwrap();
        m.add(tokens(), session("b", 60)).unwrap();
        assert_eq!(m.active().unwrap().uuid, "b");
        m.set_active("a").unwrap();

        // Tokens never touch the account file.
        let on_disk = std::fs::read_to_string(&file).unwrap();
        assert!(!on_disk.contains("mc-a"));
        assert!(!on_disk.contains("\"r\""));

        let reloaded = AccountManager::load(file, store.clone(), auth);
        assert_eq!(reloaded.active().unwrap().uuid, "a");

        let (launch, kind) = m.session("a").await.unwrap();
        assert_eq!(launch.access_token, "mc-a");
        assert!(matches!(kind, SessionKind::Online));

        m.remove("a").unwrap();
        assert_eq!(m.active().unwrap().uuid, "b");
        assert!(store.get("account:a").is_none());
    }
}
