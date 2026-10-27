//! Building the Java command line from a resolved version.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::install::InstalledVersion;
use crate::rules::Environment;
use crate::version::Argument;

/// Identity passed to the game.
#[derive(Clone, Debug)]
pub struct LaunchAuth {
    pub player_name: String,
    /// Profile UUID without dashes.
    pub uuid: String,
    pub access_token: String,
    pub xuid: String,
    pub user_type: String,
}

impl LaunchAuth {
    /// Placeholder identity used in tests and for previews of the command.
    pub fn dummy(name: &str) -> Self {
        Self {
            player_name: name.to_owned(),
            uuid: "00000000000000000000000000000000".into(),
            access_token: "0".into(),
            xuid: String::new(),
            user_type: "msa".into(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct LaunchOptions {
    pub java: PathBuf,
    pub game_dir: PathBuf,
    pub assets_dir: PathBuf,
    pub libraries_dir: PathBuf,
    pub auth: LaunchAuth,
    pub min_memory_mb: Option<u32>,
    pub max_memory_mb: u32,
    pub extra_jvm_args: Vec<String>,
    pub resolution: Option<(u32, u32)>,
    pub launcher_name: String,
    pub launcher_version: String,
    pub client_id: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Command {
    pub program: PathBuf,
    pub args: Vec<String>,
}

impl Command {
    /// Arguments with the access token masked, safe to log.
    pub fn redacted(&self, token: &str) -> Vec<String> {
        self.args
            .iter()
            .map(|a| {
                if !token.is_empty() && a.contains(token) {
                    a.replace(token, "<redacted>")
                } else {
                    a.clone()
                }
            })
            .collect()
    }
}

pub const CLASSPATH_SEPARATOR: &str = if cfg!(windows) { ";" } else { ":" };

fn path_str(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

/// Replace every `${name}` with its value; unknown placeholders are kept.
pub fn substitute(input: &str, vars: &HashMap<&str, String>) -> String {
    let mut out = String::with_capacity(input.len());
    let mut rest = input;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        match after.find('}') {
            Some(end) => {
                let key = &after[..end];
                match vars.get(key) {
                    Some(v) => out.push_str(v),
                    None => {
                        out.push_str("${");
                        out.push_str(key);
                        out.push('}');
                    }
                }
                rest = &after[end + 1..];
            }
            None => {
                out.push_str(&rest[start..]);
                rest = "";
            }
        }
    }
    out.push_str(rest);
    out
}

fn collect(args: &[Argument], env: &Environment, vars: &HashMap<&str, String>) -> Vec<String> {
    let mut out = Vec::new();
    for arg in args {
        match arg {
            Argument::Plain(s) => out.push(substitute(s, vars)),
            Argument::Conditional { rules, value } => {
                if env.allows(Some(rules)) {
                    out.extend(value.iter().map(|s| substitute(s, vars)));
                }
            }
        }
    }
    out
}

/// Build the full command. `env.features` must already reflect the options
/// (e.g. `has_custom_resolution`).
pub fn build(installed: &InstalledVersion, opts: &LaunchOptions, env: &Environment) -> Command {
    let version = &installed.version;
    let classpath = installed
        .classpath
        .iter()
        .map(|p| path_str(p))
        .collect::<Vec<_>>()
        .join(CLASSPATH_SEPARATOR);
    let (width, height) = opts.resolution.unwrap_or((854, 480));

    let vars: HashMap<&str, String> = HashMap::from([
        ("auth_player_name", opts.auth.player_name.clone()),
        ("version_name", version.id.clone()),
        ("game_directory", path_str(&opts.game_dir)),
        ("assets_root", path_str(&opts.assets_dir)),
        ("game_assets", path_str(&installed.game_assets)),
        ("assets_index_name", version.asset_index_id().to_owned()),
        ("auth_uuid", opts.auth.uuid.clone()),
        ("auth_access_token", opts.auth.access_token.clone()),
        (
            "auth_session",
            format!("token:{}:{}", opts.auth.access_token, opts.auth.uuid),
        ),
        ("auth_xuid", opts.auth.xuid.clone()),
        ("clientid", opts.client_id.clone()),
        ("user_type", opts.auth.user_type.clone()),
        ("user_properties", "{}".into()),
        ("version_type", version_type_name(version.kind).into()),
        ("resolution_width", width.to_string()),
        ("resolution_height", height.to_string()),
        ("natives_directory", path_str(&installed.natives_dir)),
        ("launcher_name", opts.launcher_name.clone()),
        ("launcher_version", opts.launcher_version.clone()),
        ("classpath", classpath),
        ("classpath_separator", CLASSPATH_SEPARATOR.into()),
        ("library_directory", path_str(&opts.libraries_dir)),
        ("primary_jar", path_str(&installed.client_jar)),
    ]);

    let mut args = Vec::new();
    if let Some(min) = opts.min_memory_mb {
        args.push(format!("-Xms{min}M"));
    }
    args.push(format!("-Xmx{}M", opts.max_memory_mb));

    if let Some(logging) = version.logging.as_ref().and_then(|l| l.client.as_ref())
        && let Some(path) = &installed.logging_config
    {
        let vars = HashMap::from([("path", path_str(path))]);
        args.push(substitute(&logging.argument, &vars));
    }
    // Defence in depth against CVE-2021-44228 for versions shipping a log4j
    // that understands the flag.
    args.push("-Dlog4j2.formatMsgNoLookups=true".into());

    match version.arguments.as_ref().filter(|a| !a.jvm.is_empty()) {
        Some(a) => args.extend(collect(&a.jvm, env, &vars)),
        None => {
            // Pre-1.13 versions don't describe JVM arguments.
            if env.os_name == "osx" {
                args.push("-XstartOnFirstThread".into());
            }
            if env.os_name == "windows" {
                args.push(
                    "-XX:HeapDumpPath=MojangTricksIntelDriversForPerformance_javaw.exe_minecraft.exe.heapdump"
                        .into(),
                );
            }
            if env.arch == "x86" {
                args.push("-Xss1M".into());
            }
            args.push(substitute("-Djava.library.path=${natives_directory}", &vars));
            args.push(substitute("-Dminecraft.launcher.brand=${launcher_name}", &vars));
            args.push(substitute(
                "-Dminecraft.launcher.version=${launcher_version}",
                &vars,
            ));
            args.push("-cp".into());
            args.push(vars["classpath"].clone());
        }
    }
    args.extend(opts.extra_jvm_args.iter().filter(|a| !a.is_empty()).cloned());
    args.push(version.main_class.clone());

    match (&version.arguments, &version.minecraft_arguments) {
        (Some(a), _) if !a.game.is_empty() => args.extend(collect(&a.game, env, &vars)),
        (_, Some(legacy)) => {
            args.extend(legacy.split_whitespace().map(|s| substitute(s, &vars)));
            if opts.resolution.is_some() {
                args.extend([
                    "--width".into(),
                    width.to_string(),
                    "--height".into(),
                    height.to_string(),
                ]);
            }
        }
        _ => {}
    }

    Command {
        program: opts.java.clone(),
        args,
    }
}

fn version_type_name(kind: crate::version::VersionType) -> &'static str {
    use crate::version::VersionType::*;
    match kind {
        Release => "release",
        Snapshot => "snapshot",
        OldBeta => "old_beta",
        OldAlpha => "old_alpha",
        Other => "release",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rules::Features;
    use crate::version::VersionJson;

    fn env() -> Environment {
        Environment {
            os_name: "linux",
            arch: "x86_64",
            os_version: String::new(),
            features: Features::default(),
        }
    }

    fn installed(json: &str) -> InstalledVersion {
        let version: VersionJson = serde_json::from_str(json).unwrap();
        InstalledVersion {
            client_jar: PathBuf::from("/d/versions/v/v.jar"),
            classpath: vec![
                PathBuf::from("/d/libraries/a.jar"),
                PathBuf::from("/d/versions/v/v.jar"),
            ],
            natives_dir: PathBuf::from("/d/natives/v"),
            game_assets: PathBuf::from("/d/assets/virtual/legacy"),
            map_to_resources: false,
            logging_config: None,
            version,
        }
    }

    fn opts() -> LaunchOptions {
        LaunchOptions {
            java: "/usr/bin/java".into(),
            game_dir: "/g".into(),
            assets_dir: "/d/assets".into(),
            libraries_dir: "/d/libraries".into(),
            auth: LaunchAuth {
                player_name: "Steve".into(),
                uuid: "abc".into(),
                access_token: "SECRET".into(),
                xuid: "123".into(),
                user_type: "msa".into(),
            },
            min_memory_mb: None,
            max_memory_mb: 4096,
            extra_jvm_args: vec!["-XX:+UseZGC".into()],
            resolution: None,
            launcher_name: "vanta".into(),
            launcher_version: "0.1.0".into(),
            client_id: String::new(),
        }
    }

    #[test]
    fn substitutes_placeholders() {
        let vars = HashMap::from([("a", "1".to_string()), ("b", "2".to_string())]);
        assert_eq!(substitute("x${a}y${b}${c}", &vars), "x1y2${c}");
        assert_eq!(substitute("open ${a", &vars), "open ${a");
    }

    #[test]
    fn modern_arguments() {
        let v = installed(
            r#"{"id":"1.20.1","mainClass":"net.minecraft.client.main.Main","assets":"5",
            "arguments":{
              "game":["--username","${auth_player_name}","--accessToken","${auth_access_token}",
                {"rules":[{"action":"allow","features":{"is_demo_user":true}}],"value":"--demo"},
                {"rules":[{"action":"allow","features":{"has_custom_resolution":true}}],"value":["--width","${resolution_width}","--height","${resolution_height}"]}],
              "jvm":[{"rules":[{"action":"allow","os":{"name":"osx"}}],"value":["-XstartOnFirstThread"]},
                "-Djava.library.path=${natives_directory}","-cp","${classpath}"]}}"#,
        );
        let cmd = build(&v, &opts(), &env());
        assert_eq!(
            cmd.args,
            vec![
                "-Xmx4096M",
                "-Dlog4j2.formatMsgNoLookups=true",
                "-Djava.library.path=/d/natives/v",
                "-cp",
                &format!("/d/libraries/a.jar{CLASSPATH_SEPARATOR}/d/versions/v/v.jar"),
                "-XX:+UseZGC",
                "net.minecraft.client.main.Main",
                "--username",
                "Steve",
                "--accessToken",
                "SECRET",
            ]
        );
        assert!(cmd.redacted("SECRET").iter().all(|a| !a.contains("SECRET")));
    }

    #[test]
    fn custom_resolution_feature() {
        let v = installed(
            r#"{"id":"1.20.1","mainClass":"M","arguments":{"game":[
                {"rules":[{"action":"allow","features":{"has_custom_resolution":true}}],"value":["--width","${resolution_width}","--height","${resolution_height}"]}],"jvm":["-cp","${classpath}"]}}"#,
        );
        let mut o = opts();
        o.resolution = Some((1280, 720));
        let mut e = env();
        e.features.has_custom_resolution = true;
        let cmd = build(&v, &o, &e);
        assert!(
            cmd.args
                .ends_with(&["--width".into(), "1280".into(), "--height".into(), "720".into()])
        );
    }

    #[test]
    fn legacy_arguments() {
        let v = installed(
            r#"{"id":"1.7.10","mainClass":"net.minecraft.client.main.Main","assets":"1.7.10",
            "minecraftArguments":"--username ${auth_player_name} --session ${auth_session} --assetsDir ${assets_root} --userProperties ${user_properties}"}"#,
        );
        let cmd = build(&v, &opts(), &env());
        let main = cmd
            .args
            .iter()
            .position(|a| a == "net.minecraft.client.main.Main")
            .unwrap();
        assert!(cmd.args[..main].contains(&"-Djava.library.path=/d/natives/v".to_string()));
        assert_eq!(
            &cmd.args[main + 1..],
            &[
                "--username",
                "Steve",
                "--session",
                "token:SECRET:abc",
                "--assetsDir",
                "/d/assets",
                "--userProperties",
                "{}"
            ]
        );
    }

    #[test]
    fn min_memory_and_logging() {
        let mut v = installed(
            r#"{"id":"1.12.2","mainClass":"M","minecraftArguments":"",
            "logging":{"client":{"argument":"-Dlog4j.configurationFile=${path}","type":"log4j2-xml",
              "file":{"id":"client-1.12.xml","sha1":"x","size":1,"url":"https://x"}}}}"#,
        );
        v.logging_config = Some("/d/assets/log_configs/client-1.12.xml".into());
        let mut o = opts();
        o.min_memory_mb = Some(512);
        let cmd = build(&v, &o, &env());
        assert_eq!(cmd.args[0], "-Xms512M");
        assert!(
            cmd.args
                .contains(&"-Dlog4j.configurationFile=/d/assets/log_configs/client-1.12.xml".to_string())
        );
    }
}
