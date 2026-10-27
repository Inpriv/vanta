//! Microsoft account sign-in: device code flow → Xbox Live → XSTS →
//! Minecraft Services, plus ownership verification.

use std::time::Duration;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum AuthError {
    #[error("no Microsoft client ID was configured for this build")]
    MissingClientId,
    #[error("network error: {0}")]
    Network(#[from] reqwest::Error),
    #[error("the sign-in code expired, please try again")]
    Expired,
    #[error("sign-in was declined")]
    Declined,
    #[error("this Microsoft account has no Xbox profile; create one at xbox.com and try again")]
    NoXboxAccount,
    #[error("Xbox Live is not available in your country")]
    CountryUnavailable,
    #[error("this account needs adult verification on xbox.com")]
    AdultVerification,
    #[error("this is a child account; it must be added to a Microsoft family by an adult")]
    ChildAccount,
    #[error("this account does not own Minecraft: Java Edition")]
    NotOwned,
    #[error("this account has no Minecraft profile yet; set a username at minecraft.net first")]
    NoProfile,
    #[error("the session expired, please sign in again")]
    SessionExpired,
    #[error("unexpected response from {service}: {detail}")]
    Unexpected { service: &'static str, detail: String },
}

type Result<T> = std::result::Result<T, AuthError>;

/// Service endpoints; overridable for tests.
#[derive(Clone, Debug)]
pub struct Endpoints {
    pub device_code: String,
    pub token: String,
    pub xbl: String,
    pub xsts: String,
    pub mc_login: String,
    pub mc_entitlements: String,
    pub mc_profile: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Self {
            device_code: "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode".into(),
            token: "https://login.microsoftonline.com/consumers/oauth2/v2.0/token".into(),
            xbl: "https://user.auth.xboxlive.com/user/authenticate".into(),
            xsts: "https://xsts.auth.xboxlive.com/xsts/authorize".into(),
            mc_login: "https://api.minecraftservices.com/authentication/login_with_xbox".into(),
            mc_entitlements: "https://api.minecraftservices.com/entitlements/mcstore".into(),
            mc_profile: "https://api.minecraftservices.com/minecraft/profile".into(),
        }
    }
}

const SCOPE: &str = "XboxLive.signin offline_access";

#[derive(Clone, Debug, Deserialize)]
pub struct DeviceCode {
    pub device_code: String,
    pub user_code: String,
    pub verification_uri: String,
    pub expires_in: u64,
    #[serde(default = "default_interval")]
    pub interval: u64,
    #[serde(default)]
    pub message: String,
}

fn default_interval() -> u64 {
    5
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MsaTokens {
    pub access_token: String,
    pub refresh_token: String,
    pub expires_at: DateTime<Utc>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Skin {
    pub url: String,
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub variant: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Profile {
    /// UUID without dashes.
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub skins: Vec<Skin>,
}

impl Profile {
    pub fn active_skin(&self) -> Option<&Skin> {
        self.skins
            .iter()
            .find(|s| s.state.eq_ignore_ascii_case("active"))
            .or(self.skins.first())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MinecraftSession {
    pub access_token: String,
    pub expires_at: DateTime<Utc>,
    pub profile: Profile,
    pub xuid: String,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    #[serde(default)]
    refresh_token: Option<String>,
    expires_in: i64,
}

#[derive(Deserialize)]
struct OAuthError {
    error: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct XboxResponse {
    token: String,
    display_claims: XboxClaims,
}

#[derive(Deserialize)]
struct XboxClaims {
    xui: Vec<XboxUser>,
}

#[derive(Deserialize)]
struct XboxUser {
    uhs: String,
    #[serde(default)]
    xid: Option<String>,
}

#[derive(Deserialize)]
struct XErr {
    #[serde(rename = "XErr")]
    xerr: u64,
}

#[derive(Deserialize)]
struct McLogin {
    access_token: String,
    expires_in: i64,
}

#[derive(Deserialize)]
struct Entitlements {
    #[serde(default)]
    items: Vec<EntitlementItem>,
}

#[derive(Deserialize)]
struct EntitlementItem {
    name: String,
}

#[derive(Clone)]
pub struct AuthClient {
    client: reqwest::Client,
    client_id: String,
    endpoints: Endpoints,
}

fn unexpected(service: &'static str, detail: impl ToString) -> AuthError {
    AuthError::Unexpected {
        service,
        detail: detail.to_string(),
    }
}

impl AuthClient {
    pub fn new(client: reqwest::Client, client_id: impl Into<String>) -> Self {
        Self {
            client,
            client_id: client_id.into(),
            endpoints: Endpoints::default(),
        }
    }

    pub fn with_endpoints(mut self, endpoints: Endpoints) -> Self {
        self.endpoints = endpoints;
        self
    }

    fn check_client_id(&self) -> Result<()> {
        if self.client_id.trim().is_empty() {
            Err(AuthError::MissingClientId)
        } else {
            Ok(())
        }
    }

    pub async fn start_device_code(&self) -> Result<DeviceCode> {
        self.check_client_id()?;
        let res = self
            .client
            .post(&self.endpoints.device_code)
            .form(&[("client_id", self.client_id.as_str()), ("scope", SCOPE)])
            .send()
            .await?;
        if !res.status().is_success() {
            return Err(unexpected("Microsoft", res.text().await.unwrap_or_default()));
        }
        Ok(res.json().await?)
    }

    /// Poll until the user finishes signing in in their browser.
    pub async fn poll_device_code(&self, code: &DeviceCode) -> Result<MsaTokens> {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(code.expires_in);
        let mut interval = code.interval.max(1);
        loop {
            tokio::time::sleep(Duration::from_secs(interval)).await;
            if tokio::time::Instant::now() >= deadline {
                return Err(AuthError::Expired);
            }
            let res = self
                .client
                .post(&self.endpoints.token)
                .form(&[
                    ("grant_type", "urn:ietf:params:oauth:grant-type:device_code"),
                    ("client_id", self.client_id.as_str()),
                    ("device_code", code.device_code.as_str()),
                ])
                .send()
                .await?;
            if res.status().is_success() {
                return parse_tokens(res.json().await?, None);
            }
            let body = res.text().await.unwrap_or_default();
            let err = serde_json::from_str::<OAuthError>(&body)
                .map(|e| e.error)
                .unwrap_or_default();
            match err.as_str() {
                "authorization_pending" => {}
                "slow_down" => interval += 5,
                "expired_token" => return Err(AuthError::Expired),
                "authorization_declined" | "access_denied" => return Err(AuthError::Declined),
                _ => return Err(unexpected("Microsoft", body)),
            }
        }
    }

    pub async fn refresh(&self, refresh_token: &str) -> Result<MsaTokens> {
        self.check_client_id()?;
        let res = self
            .client
            .post(&self.endpoints.token)
            .form(&[
                ("grant_type", "refresh_token"),
                ("client_id", self.client_id.as_str()),
                ("refresh_token", refresh_token),
                ("scope", SCOPE),
            ])
            .send()
            .await?;
        if res.status().as_u16() == 400 || res.status().as_u16() == 401 {
            return Err(AuthError::SessionExpired);
        }
        if !res.status().is_success() {
            return Err(unexpected("Microsoft", res.status()));
        }
        parse_tokens(res.json().await?, Some(refresh_token))
    }

    /// Exchange a Microsoft access token for a Minecraft session, verifying
    /// that the account owns the game.
    pub async fn login_minecraft(&self, msa_access_token: &str) -> Result<MinecraftSession> {
        let xbl: XboxResponse = {
            let res = self
                .client
                .post(&self.endpoints.xbl)
                .header("Accept", "application/json")
                .json(&json!({
                    "Properties": {
                        "AuthMethod": "RPS",
                        "SiteName": "user.auth.xboxlive.com",
                        "RpsTicket": format!("d={msa_access_token}"),
                    },
                    "RelyingParty": "http://auth.xboxlive.com",
                    "TokenType": "JWT",
                }))
                .send()
                .await?;
            if !res.status().is_success() {
                return Err(unexpected("Xbox Live", res.status()));
            }
            res.json().await?
        };

        let xsts: XboxResponse = {
            let res = self
                .client
                .post(&self.endpoints.xsts)
                .header("Accept", "application/json")
                .json(&json!({
                    "Properties": { "SandboxId": "RETAIL", "UserTokens": [xbl.token] },
                    "RelyingParty": "rp://api.minecraftservices.com/",
                    "TokenType": "JWT",
                }))
                .send()
                .await?;
            if res.status().as_u16() == 401 {
                let body = res.text().await.unwrap_or_default();
                return Err(match serde_json::from_str::<XErr>(&body).map(|e| e.xerr) {
                    Ok(2148916233) => AuthError::NoXboxAccount,
                    Ok(2148916235) => AuthError::CountryUnavailable,
                    Ok(2148916236 | 2148916237) => AuthError::AdultVerification,
                    Ok(2148916238) => AuthError::ChildAccount,
                    _ => unexpected("XSTS", body),
                });
            }
            if !res.status().is_success() {
                return Err(unexpected("XSTS", res.status()));
            }
            res.json().await?
        };
        let user = xsts
            .display_claims
            .xui
            .first()
            .ok_or_else(|| unexpected("XSTS", "missing user hash"))?;

        let mc: McLogin = {
            let res = self
                .client
                .post(&self.endpoints.mc_login)
                .json(&json!({ "identityToken": format!("XBL3.0 x={};{}", user.uhs, xsts.token) }))
                .send()
                .await?;
            if !res.status().is_success() {
                return Err(unexpected("Minecraft Services", res.status()));
            }
            res.json().await?
        };

        let entitlements: Entitlements = self
            .client
            .get(&self.endpoints.mc_entitlements)
            .bearer_auth(&mc.access_token)
            .send()
            .await?
            .json()
            .await
            .unwrap_or(Entitlements { items: Vec::new() });

        let res = self
            .client
            .get(&self.endpoints.mc_profile)
            .bearer_auth(&mc.access_token)
            .send()
            .await?;
        let profile: Option<Profile> = match res.status().as_u16() {
            200 => Some(res.json().await?),
            404 => None,
            s => return Err(unexpected("Minecraft Services", s)),
        };

        let owns = entitlements
            .items
            .iter()
            .any(|i| i.name == "product_minecraft" || i.name == "game_minecraft");
        // Game Pass accounts can have an empty entitlement list but still get a
        // profile, which is only issued to accounts that may play.
        let profile = match (profile, owns) {
            (Some(p), _) => p,
            (None, true) => return Err(AuthError::NoProfile),
            (None, false) => return Err(AuthError::NotOwned),
        };

        Ok(MinecraftSession {
            access_token: mc.access_token,
            expires_at: Utc::now() + chrono::Duration::seconds(mc.expires_in),
            profile,
            xuid: user.xid.clone().unwrap_or_default(),
        })
    }
}

fn parse_tokens(t: TokenResponse, old_refresh: Option<&str>) -> Result<MsaTokens> {
    let refresh_token = t
        .refresh_token
        .or_else(|| old_refresh.map(str::to_owned))
        .ok_or_else(|| unexpected("Microsoft", "no refresh token"))?;
    Ok(MsaTokens {
        access_token: t.access_token,
        refresh_token,
        expires_at: Utc::now() + chrono::Duration::seconds(t.expires_in),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_string_contains, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn endpoints(base: &str) -> Endpoints {
        Endpoints {
            device_code: format!("{base}/devicecode"),
            token: format!("{base}/token"),
            xbl: format!("{base}/xbl"),
            xsts: format!("{base}/xsts"),
            mc_login: format!("{base}/mc/login"),
            mc_entitlements: format!("{base}/mc/entitlements"),
            mc_profile: format!("{base}/mc/profile"),
        }
    }

    async fn mount_xbox(server: &MockServer) {
        Mock::given(method("POST"))
            .and(path("/xbl"))
            .and(body_string_contains("d=MSA"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "Token": "XBL", "DisplayClaims": {"xui": [{"uhs": "HASH"}]}
            })))
            .mount(server)
            .await;
        Mock::given(method("POST"))
            .and(path("/xsts"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "Token": "XSTS", "DisplayClaims": {"xui": [{"uhs": "HASH"}]}
            })))
            .mount(server)
            .await;
        Mock::given(method("POST"))
            .and(path("/mc/login"))
            .and(body_string_contains("XBL3.0 x=HASH;XSTS"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "access_token": "MC", "expires_in": 86400
            })))
            .mount(server)
            .await;
    }

    #[tokio::test]
    async fn full_login_chain() {
        let server = MockServer::start().await;
        mount_xbox(&server).await;
        Mock::given(method("GET"))
            .and(path("/mc/entitlements"))
            .and(header("authorization", "Bearer MC"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "items": [{"name": "product_minecraft"}, {"name": "game_minecraft"}]
            })))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/mc/profile"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch",
                "skins": [{"id": "1", "state": "ACTIVE", "url": "https://textures/x", "variant": "CLASSIC"}]
            })))
            .mount(&server)
            .await;

        let auth = AuthClient::new(crate::http::client(), "cid").with_endpoints(endpoints(&server.uri()));
        let session = auth.login_minecraft("MSA").await.unwrap();
        assert_eq!(session.access_token, "MC");
        assert_eq!(session.profile.name, "Notch");
        assert_eq!(session.profile.active_skin().unwrap().url, "https://textures/x");
    }

    #[tokio::test]
    async fn not_owned() {
        let server = MockServer::start().await;
        mount_xbox(&server).await;
        Mock::given(method("GET"))
            .and(path("/mc/entitlements"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"items": []})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/mc/profile"))
            .respond_with(ResponseTemplate::new(404))
            .mount(&server)
            .await;
        let auth = AuthClient::new(crate::http::client(), "cid").with_endpoints(endpoints(&server.uri()));
        assert!(matches!(
            auth.login_minecraft("MSA").await,
            Err(AuthError::NotOwned)
        ));
    }

    #[tokio::test]
    async fn xsts_errors_are_friendly() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/xbl"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "Token": "XBL", "DisplayClaims": {"xui": [{"uhs": "HASH"}]}
            })))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/xsts"))
            .respond_with(ResponseTemplate::new(401).set_body_json(json!({"XErr": 2148916238u64})))
            .mount(&server)
            .await;
        let auth = AuthClient::new(crate::http::client(), "cid").with_endpoints(endpoints(&server.uri()));
        assert!(matches!(
            auth.login_minecraft("MSA").await,
            Err(AuthError::ChildAccount)
        ));
    }

    #[tokio::test]
    async fn device_code_flow_polls_until_done() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/devicecode"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "device_code": "DC", "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/link", "expires_in": 60, "interval": 1
            })))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/token"))
            .respond_with(ResponseTemplate::new(400).set_body_json(json!({"error": "authorization_pending"})))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/token"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "access_token": "MSA", "refresh_token": "R", "expires_in": 3600
            })))
            .mount(&server)
            .await;
        let auth = AuthClient::new(crate::http::client(), "cid").with_endpoints(endpoints(&server.uri()));
        let code = auth.start_device_code().await.unwrap();
        assert_eq!(code.user_code, "ABCD-EFGH");
        let tokens = auth.poll_device_code(&code).await.unwrap();
        assert_eq!(tokens.refresh_token, "R");
    }

    #[tokio::test]
    async fn missing_client_id() {
        let auth = AuthClient::new(crate::http::client(), "");
        assert!(matches!(
            auth.start_device_code().await,
            Err(AuthError::MissingClientId)
        ));
    }
}
