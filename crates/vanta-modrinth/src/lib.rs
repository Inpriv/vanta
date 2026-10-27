//! Minimal, typed client for the Modrinth v2 API.
//!
//! API reference: <https://docs.modrinth.com/api/>

use std::collections::{HashMap, HashSet, VecDeque};

use serde::{Deserialize, Serialize};

pub const API_URL: &str = "https://api.modrinth.com/v2";

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("network error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("Modrinth returned HTTP {status}: {body}")]
    Status { status: u16, body: String },
    #[error("invalid response: {0}")]
    Json(#[from] serde_json::Error),
    #[error("no compatible version of `{0}` was found")]
    NoCompatibleVersion(String),
}

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SortIndex {
    #[default]
    Relevance,
    Downloads,
    Follows,
    Newest,
    Updated,
}

impl SortIndex {
    fn as_str(self) -> &'static str {
        match self {
            Self::Relevance => "relevance",
            Self::Downloads => "downloads",
            Self::Follows => "follows",
            Self::Newest => "newest",
            Self::Updated => "updated",
        }
    }
}

/// Search parameters; empty filters are omitted.
#[derive(Clone, Debug, Default)]
pub struct SearchQuery {
    pub query: String,
    /// `mod`, `resourcepack`, `shader`, `modpack`.
    pub project_type: Option<String>,
    pub game_version: Option<String>,
    /// Loader or, for packs, a category such as `minecraft`/`iris`.
    pub loader: Option<String>,
    pub categories: Vec<String>,
    pub index: SortIndex,
    pub offset: u32,
    pub limit: u32,
}

impl SearchQuery {
    /// Facets in Modrinth's JSON-array-of-OR-groups format.
    pub fn facets(&self) -> String {
        let mut groups: Vec<Vec<String>> = Vec::new();
        if let Some(t) = &self.project_type {
            groups.push(vec![format!("project_type:{t}")]);
        }
        if let Some(v) = &self.game_version {
            groups.push(vec![format!("versions:{v}")]);
        }
        if let Some(l) = &self.loader {
            groups.push(vec![format!("categories:{l}")]);
        }
        for c in &self.categories {
            groups.push(vec![format!("categories:{c}")]);
        }
        serde_json::to_string(&groups).expect("facets serialize")
    }
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct SearchHit {
    pub project_id: String,
    pub slug: String,
    pub title: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub categories: Vec<String>,
    #[serde(default)]
    pub display_categories: Vec<String>,
    #[serde(default)]
    pub downloads: u64,
    #[serde(default)]
    pub follows: u64,
    #[serde(default)]
    pub icon_url: Option<String>,
    #[serde(default)]
    pub author: String,
    pub project_type: String,
    #[serde(default)]
    pub client_side: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SearchResults {
    pub hits: Vec<SearchHit>,
    pub offset: u32,
    pub limit: u32,
    pub total_hits: u32,
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct Project {
    pub id: String,
    pub slug: String,
    pub title: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub icon_url: Option<String>,
    #[serde(default)]
    pub downloads: u64,
    #[serde(default)]
    pub followers: u64,
    pub project_type: String,
    #[serde(default)]
    pub game_versions: Vec<String>,
    #[serde(default)]
    pub loaders: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DependencyType {
    Required,
    Optional,
    Incompatible,
    Embedded,
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct Dependency {
    #[serde(default)]
    pub version_id: Option<String>,
    #[serde(default)]
    pub project_id: Option<String>,
    #[serde(default)]
    pub file_name: Option<String>,
    pub dependency_type: DependencyType,
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct Hashes {
    pub sha1: String,
    #[serde(default)]
    pub sha512: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct VersionFile {
    pub hashes: Hashes,
    pub url: String,
    pub filename: String,
    #[serde(default)]
    pub primary: bool,
    pub size: u64,
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct Version {
    pub id: String,
    pub project_id: String,
    pub name: String,
    pub version_number: String,
    #[serde(default)]
    pub dependencies: Vec<Dependency>,
    #[serde(default)]
    pub game_versions: Vec<String>,
    #[serde(default)]
    pub version_type: String,
    #[serde(default)]
    pub loaders: Vec<String>,
    #[serde(default)]
    pub date_published: String,
    pub files: Vec<VersionFile>,
}

impl Version {
    /// The primary file, or the first one.
    pub fn primary_file(&self) -> Option<&VersionFile> {
        self.files.iter().find(|f| f.primary).or(self.files.first())
    }
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct Category {
    pub name: String,
    pub project_type: String,
    #[serde(default)]
    pub header: String,
}

#[derive(Clone, Debug)]
pub struct Client {
    http: reqwest::Client,
    base: String,
}

impl Client {
    pub fn new(http: reqwest::Client) -> Self {
        Self::with_base(http, API_URL)
    }

    pub fn with_base(http: reqwest::Client, base: impl Into<String>) -> Self {
        Self {
            http,
            base: base.into().trim_end_matches('/').to_owned(),
        }
    }

    async fn send<T: serde::de::DeserializeOwned>(&self, req: reqwest::RequestBuilder) -> Result<T> {
        let res = req.send().await?;
        let status = res.status();
        let bytes = res.bytes().await?;
        if !status.is_success() {
            return Err(Error::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&bytes).chars().take(300).collect(),
            });
        }
        Ok(serde_json::from_slice(&bytes)?)
    }

    pub async fn search(&self, q: &SearchQuery) -> Result<SearchResults> {
        let limit = if q.limit == 0 { 20 } else { q.limit.min(100) };
        let mut params = vec![
            ("index", q.index.as_str().to_owned()),
            ("offset", q.offset.to_string()),
            ("limit", limit.to_string()),
            ("facets", q.facets()),
        ];
        if !q.query.trim().is_empty() {
            params.push(("query", q.query.trim().to_owned()));
        }
        self.send(self.http.get(format!("{}/search", self.base)).query(&params))
            .await
    }

    pub async fn project(&self, id_or_slug: &str) -> Result<Project> {
        self.send(self.http.get(format!("{}/project/{id_or_slug}", self.base)))
            .await
    }

    pub async fn projects(&self, ids: &[String]) -> Result<Vec<Project>> {
        if ids.is_empty() {
            return Ok(Vec::new());
        }
        let ids = serde_json::to_string(ids)?;
        self.send(
            self.http
                .get(format!("{}/projects", self.base))
                .query(&[("ids", ids)]),
        )
        .await
    }

    /// Versions of a project, newest first, optionally filtered.
    pub async fn project_versions(
        &self,
        project: &str,
        loaders: &[&str],
        game_versions: &[&str],
    ) -> Result<Vec<Version>> {
        let mut params = Vec::new();
        if !loaders.is_empty() {
            params.push(("loaders", serde_json::to_string(loaders)?));
        }
        if !game_versions.is_empty() {
            params.push(("game_versions", serde_json::to_string(game_versions)?));
        }
        self.send(
            self.http
                .get(format!("{}/project/{project}/version", self.base))
                .query(&params),
        )
        .await
    }

    pub async fn version(&self, id: &str) -> Result<Version> {
        self.send(self.http.get(format!("{}/version/{id}", self.base)))
            .await
    }

    /// Look up versions by file SHA-1.
    pub async fn versions_by_hashes(&self, sha1s: &[String]) -> Result<HashMap<String, Version>> {
        if sha1s.is_empty() {
            return Ok(HashMap::new());
        }
        self.send(
            self.http
                .post(format!("{}/version_files", self.base))
                .json(&serde_json::json!({ "hashes": sha1s, "algorithm": "sha1" })),
        )
        .await
    }

    /// Latest compatible version for each installed file (keyed by SHA-1).
    pub async fn latest_versions(
        &self,
        sha1s: &[String],
        loaders: &[&str],
        game_versions: &[&str],
    ) -> Result<HashMap<String, Version>> {
        if sha1s.is_empty() {
            return Ok(HashMap::new());
        }
        let mut body = serde_json::json!({ "hashes": sha1s, "algorithm": "sha1" });
        if !loaders.is_empty() {
            body["loaders"] = serde_json::json!(loaders);
        }
        if !game_versions.is_empty() {
            body["game_versions"] = serde_json::json!(game_versions);
        }
        self.send(
            self.http
                .post(format!("{}/version_files/update", self.base))
                .json(&body),
        )
        .await
    }

    pub async fn categories(&self) -> Result<Vec<Category>> {
        self.send(self.http.get(format!("{}/tag/category", self.base)))
            .await
    }

    /// Newest compatible version of a project.
    pub async fn best_version(&self, project: &str, loaders: &[&str], game_version: &str) -> Result<Version> {
        let versions = self.project_versions(project, loaders, &[game_version]).await?;
        // Prefer releases over betas/alphas when one exists.
        versions
            .iter()
            .find(|v| v.version_type == "release")
            .or(versions.first())
            .cloned()
            .ok_or_else(|| Error::NoCompatibleVersion(project.to_owned()))
    }

    /// `root` plus every required dependency that is not already installed,
    /// in install order (dependencies before dependents is not required by
    /// any loader, so order is breadth-first from the root).
    pub async fn resolve_with_dependencies(
        &self,
        root: Version,
        loaders: &[&str],
        game_version: &str,
        installed_projects: &HashSet<String>,
    ) -> Result<Vec<Version>> {
        let mut out = Vec::new();
        let mut seen: HashSet<String> = installed_projects.clone();
        seen.insert(root.project_id.clone());
        let mut queue = VecDeque::from([root]);
        while let Some(v) = queue.pop_front() {
            for dep in v
                .dependencies
                .iter()
                .filter(|d| d.dependency_type == DependencyType::Required)
            {
                let next = match (&dep.version_id, &dep.project_id) {
                    (Some(vid), _) => {
                        let dv = self.version(vid).await?;
                        if seen.contains(&dv.project_id) {
                            continue;
                        }
                        dv
                    }
                    (None, Some(pid)) => {
                        if seen.contains(pid) {
                            continue;
                        }
                        self.best_version(pid, loaders, game_version).await?
                    }
                    (None, None) => continue,
                };
                seen.insert(next.project_id.clone());
                queue.push_back(next);
            }
            out.push(v);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{body_partial_json, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn http() -> reqwest::Client {
        let _ = rustls::crypto::ring::default_provider().install_default();
        reqwest::Client::new()
    }

    fn version(id: &str, project: &str, deps: serde_json::Value) -> serde_json::Value {
        json!({
            "id": id, "project_id": project, "name": id, "version_number": "1.0.0",
            "dependencies": deps, "game_versions": ["1.20.1"], "version_type": "release",
            "loaders": ["fabric"], "date_published": "2024-01-01T00:00:00Z",
            "files": [{"hashes": {"sha1": format!("sha-{id}"), "sha512": "x"},
                       "url": format!("https://cdn.modrinth.com/{id}.jar"),
                       "filename": format!("{id}.jar"), "primary": true, "size": 10}]
        })
    }

    #[test]
    fn builds_facets() {
        let q = SearchQuery {
            project_type: Some("mod".into()),
            game_version: Some("1.20.1".into()),
            loader: Some("fabric".into()),
            categories: vec!["optimization".into()],
            ..Default::default()
        };
        assert_eq!(
            q.facets(),
            r#"[["project_type:mod"],["versions:1.20.1"],["categories:fabric"],["categories:optimization"]]"#
        );
        assert_eq!(SearchQuery::default().facets(), "[]");
    }

    #[tokio::test]
    async fn search_sends_facets() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/search"))
            .and(query_param("query", "sodium"))
            .and(query_param("facets", r#"[["project_type:mod"]]"#))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "hits": [{"project_id": "AANobbMI", "slug": "sodium", "title": "Sodium",
                          "description": "Fast", "downloads": 10, "project_type": "mod", "author": "jellysquid3"}],
                "offset": 0, "limit": 20, "total_hits": 1
            })))
            .mount(&server)
            .await;
        let client = Client::with_base(http(), server.uri());
        let res = client
            .search(&SearchQuery {
                query: "sodium".into(),
                project_type: Some("mod".into()),
                ..Default::default()
            })
            .await
            .unwrap();
        assert_eq!(res.total_hits, 1);
        assert_eq!(res.hits[0].title, "Sodium");
    }

    #[tokio::test]
    async fn resolves_required_dependencies_once() {
        let server = MockServer::start().await;
        // root → api (required, by project) and lib (required, by version)
        // api  → lib (required)  — lib must appear once
        // root → extra (optional) — skipped
        Mock::given(method("GET"))
            .and(path("/project/api/version"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([version(
                "api-1",
                "api",
                json!([{"project_id": "lib", "dependency_type": "required"}])
            )])))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/version/lib-1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(version("lib-1", "lib", json!([]))))
            .mount(&server)
            .await;
        let client = Client::with_base(http(), server.uri());
        let root: Version = serde_json::from_value(version(
            "root-1",
            "root",
            json!([
                {"project_id": "api", "dependency_type": "required"},
                {"version_id": "lib-1", "dependency_type": "required"},
                {"project_id": "extra", "dependency_type": "optional"}
            ]),
        ))
        .unwrap();
        let all = client
            .resolve_with_dependencies(root, &["fabric"], "1.20.1", &HashSet::new())
            .await
            .unwrap();
        let ids: Vec<_> = all.iter().map(|v| v.id.as_str()).collect();
        assert_eq!(ids, vec!["root-1", "api-1", "lib-1"]);

        // Already-installed dependencies are skipped.
        let root: Version = serde_json::from_value(version(
            "root-1",
            "root",
            json!([{"project_id": "api", "dependency_type": "required"}]),
        ))
        .unwrap();
        let installed = HashSet::from(["api".to_string()]);
        let all = client
            .resolve_with_dependencies(root, &["fabric"], "1.20.1", &installed)
            .await
            .unwrap();
        assert_eq!(all.len(), 1);
    }

    #[tokio::test]
    async fn update_lookup_by_hash() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/version_files/update"))
            .and(body_partial_json(
                json!({"algorithm": "sha1", "loaders": ["fabric"]}),
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "oldsha": version("new-1", "p", json!([]))
            })))
            .mount(&server)
            .await;
        let client = Client::with_base(http(), server.uri());
        let res = client
            .latest_versions(&["oldsha".into()], &["fabric"], &["1.20.1"])
            .await
            .unwrap();
        assert_eq!(res["oldsha"].id, "new-1");
        assert_eq!(res["oldsha"].primary_file().unwrap().filename, "new-1.jar");
    }

    #[tokio::test]
    async fn surfaces_http_errors() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(404).set_body_string("not found"))
            .mount(&server)
            .await;
        let client = Client::with_base(http(), server.uri());
        assert!(matches!(
            client.project("nope").await,
            Err(Error::Status { status: 404, .. })
        ));
    }
}
