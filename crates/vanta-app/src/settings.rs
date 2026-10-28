//! Launcher settings, stored in the platform config directory so that the
//! data folder itself can be moved.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ThemeMode {
    #[default]
    Dark,
    Light,
    System,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CloseBehavior {
    #[default]
    KeepOpen,
    Minimize,
    Close,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub theme: ThemeMode,
    pub accent: u8,
    pub reduce_motion: bool,
    pub default_memory_mb: u32,
    pub java_path: Option<PathBuf>,
    pub jvm_args: String,
    pub close_behavior: CloseBehavior,
    pub data_dir: Option<PathBuf>,
    pub auto_update_check: bool,
    pub featured_instance: Option<String>,
    /// Render on the CPU instead of OpenGL (lower memory with some GPU drivers).
    pub software_rendering: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            theme: ThemeMode::Dark,
            accent: 3,
            reduce_motion: false,
            default_memory_mb: default_memory(),
            java_path: None,
            jvm_args: String::new(),
            close_behavior: CloseBehavior::KeepOpen,
            data_dir: None,
            auto_update_check: true,
            featured_instance: None,
            software_rendering: false,
        }
    }
}

/// A sensible default: a quarter of system memory, between 2 and 6 GB.
fn default_memory() -> u32 {
    let total = crate::sys::total_memory_mb().unwrap_or(8192);
    ((total / 4) / 512 * 512).clamp(2048, 6144)
}

pub fn config_dir() -> PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(if cfg!(target_os = "linux") {
            "vanta"
        } else {
            "Vanta"
        })
}

fn settings_path() -> PathBuf {
    config_dir().join("settings.json")
}

impl Settings {
    pub fn load() -> Self {
        std::fs::read(settings_path())
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    pub fn save(&self) -> std::io::Result<()> {
        let path = settings_path();
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(tmp, path)
    }

    pub fn data_root(&self) -> PathBuf {
        self.data_dir
            .clone()
            .unwrap_or_else(vanta_core::paths::Paths::default_root)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_and_missing_fields_fall_back_to_defaults() {
        let s: Settings = serde_json::from_str(r#"{"theme":"light","future_field":1}"#).unwrap();
        assert_eq!(s.theme, ThemeMode::Light);
        assert!(s.auto_update_check);
        assert!(s.default_memory_mb >= 2048);
    }
}
