//! Evaluation of the `rules` arrays found in version JSON files.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RuleAction {
    Allow,
    Disallow,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct OsRule {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub arch: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Rule {
    pub action: RuleAction,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os: Option<OsRule>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub features: Option<HashMap<String, bool>>,
}

/// Launcher feature flags referenced by argument rules.
#[derive(Clone, Debug, Default)]
pub struct Features {
    pub is_demo_user: bool,
    pub has_custom_resolution: bool,
    pub has_quick_plays_support: bool,
    pub is_quick_play_singleplayer: bool,
    pub is_quick_play_multiplayer: bool,
    pub is_quick_play_realms: bool,
}

impl Features {
    fn get(&self, name: &str) -> bool {
        match name {
            "is_demo_user" => self.is_demo_user,
            "has_custom_resolution" => self.has_custom_resolution,
            "has_quick_plays_support" => self.has_quick_plays_support,
            "is_quick_play_singleplayer" => self.is_quick_play_singleplayer,
            "is_quick_play_multiplayer" => self.is_quick_play_multiplayer,
            "is_quick_play_realms" => self.is_quick_play_realms,
            _ => false,
        }
    }
}

/// The platform rules are evaluated against.
#[derive(Clone, Debug)]
pub struct Environment {
    /// Mojang OS name: `windows`, `osx` or `linux`.
    pub os_name: &'static str,
    /// Rust architecture name (`x86_64`, `x86`, `aarch64`, ...).
    pub arch: &'static str,
    pub os_version: String,
    pub features: Features,
}

impl Environment {
    pub fn current() -> Self {
        let os_name = match std::env::consts::OS {
            "windows" => "windows",
            "macos" => "osx",
            _ => "linux",
        };
        // Windows 10 and 11 both report 10.0; that is all rules ever test for.
        let os_version = if os_name == "windows" { "10.0" } else { "" }.to_owned();
        Self {
            os_name,
            arch: std::env::consts::ARCH,
            os_version,
            features: Features::default(),
        }
    }

    /// Value substituted for `${arch}` in native classifiers.
    pub fn arch_bits(&self) -> &'static str {
        match self.arch {
            "x86" | "arm" => "32",
            _ => "64",
        }
    }

    fn arch_matches(&self, rule_arch: &str) -> bool {
        match rule_arch {
            "x86" => self.arch == "x86",
            "x86_64" | "amd64" => self.arch == "x86_64",
            "arm64" | "aarch64" => self.arch == "aarch64",
            "arm" | "arm32" => self.arch == "arm",
            other => other == self.arch,
        }
    }

    fn os_matches(&self, os: &OsRule) -> bool {
        if let Some(name) = &os.name
            && name != self.os_name
        {
            return false;
        }
        if let Some(arch) = &os.arch
            && !self.arch_matches(arch)
        {
            return false;
        }
        if let Some(pattern) = &os.version {
            match regex_lite::Regex::new(pattern) {
                Ok(re) if re.is_match(&self.os_version) => {}
                _ => return false,
            }
        }
        true
    }

    fn rule_matches(&self, rule: &Rule) -> bool {
        if let Some(os) = &rule.os
            && !self.os_matches(os)
        {
            return false;
        }
        if let Some(features) = &rule.features
            && features.iter().any(|(k, v)| self.features.get(k) != *v)
        {
            return false;
        }
        true
    }

    /// Mojang semantics: no rules means allowed; otherwise the last matching
    /// rule decides, and nothing matching means disallowed.
    pub fn allows(&self, rules: Option<&[Rule]>) -> bool {
        let Some(rules) = rules else { return true };
        if rules.is_empty() {
            return true;
        }
        let mut allowed = false;
        for rule in rules {
            if self.rule_matches(rule) {
                allowed = rule.action == RuleAction::Allow;
            }
        }
        allowed
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(os: &'static str, arch: &'static str) -> Environment {
        Environment {
            os_name: os,
            arch,
            os_version: if os == "windows" {
                "10.0".into()
            } else {
                String::new()
            },
            features: Features::default(),
        }
    }

    fn rules(json: &str) -> Vec<Rule> {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn no_rules_allows() {
        assert!(env("linux", "x86_64").allows(None));
        assert!(env("linux", "x86_64").allows(Some(&[])));
    }

    #[test]
    fn allow_all_except_osx() {
        let r = rules(r#"[{"action":"allow"},{"action":"disallow","os":{"name":"osx"}}]"#);
        assert!(env("linux", "x86_64").allows(Some(&r)));
        assert!(env("windows", "x86_64").allows(Some(&r)));
        assert!(!env("osx", "aarch64").allows(Some(&r)));
    }

    #[test]
    fn only_on_one_os() {
        let r = rules(r#"[{"action":"allow","os":{"name":"windows"}}]"#);
        assert!(env("windows", "x86_64").allows(Some(&r)));
        assert!(!env("linux", "x86_64").allows(Some(&r)));
    }

    #[test]
    fn arch_and_version() {
        let x86 = rules(r#"[{"action":"allow","os":{"arch":"x86"}}]"#);
        assert!(!env("windows", "x86_64").allows(Some(&x86)));
        assert!(env("windows", "x86").allows(Some(&x86)));

        let win10 = rules(r#"[{"action":"allow","os":{"name":"windows","version":"^10\\."}}]"#);
        assert!(env("windows", "x86_64").allows(Some(&win10)));
    }

    #[test]
    fn features() {
        let r = rules(r#"[{"action":"allow","features":{"has_custom_resolution":true}}]"#);
        let mut e = env("linux", "x86_64");
        assert!(!e.allows(Some(&r)));
        e.features.has_custom_resolution = true;
        assert!(e.allows(Some(&r)));

        let demo = rules(r#"[{"action":"allow","features":{"is_demo_user":true}}]"#);
        assert!(!e.allows(Some(&demo)));
    }

    #[test]
    fn arch_bits() {
        assert_eq!(env("windows", "x86_64").arch_bits(), "64");
        assert_eq!(env("windows", "x86").arch_bits(), "32");
    }
}
