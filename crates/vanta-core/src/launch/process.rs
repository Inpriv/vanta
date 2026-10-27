//! Running the game and streaming its output.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::SystemTime;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Child;
use tokio::sync::mpsc;

use super::args::Command;
use crate::error::{Error, Result};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum LogLevel {
    Trace,
    Debug,
    Info,
    Warn,
    Error,
    Fatal,
}

impl LogLevel {
    fn parse(s: &str) -> Option<Self> {
        Some(match s.to_ascii_uppercase().as_str() {
            "TRACE" => Self::Trace,
            "DEBUG" => Self::Debug,
            "INFO" => Self::Info,
            "WARN" | "WARNING" => Self::Warn,
            "ERROR" | "SEVERE" => Self::Error,
            "FATAL" => Self::Fatal,
            _ => return None,
        })
    }

    /// Best-effort level detection for plain-text lines such as
    /// `[12:00:00] [Render thread/WARN]: ...`.
    pub fn sniff(line: &str) -> Self {
        let head = &line[..line.len().min(96)];
        for (needle, level) in [
            ("/FATAL]", Self::Fatal),
            ("/ERROR]", Self::Error),
            ("/WARN]", Self::Warn),
            ("/DEBUG]", Self::Debug),
            ("[SEVERE]", Self::Error),
            ("[WARNING]", Self::Warn),
        ] {
            if head.contains(needle) {
                return level;
            }
        }
        if line.starts_with("Exception in thread") || line.starts_with("\tat ") {
            return Self::Error;
        }
        Self::Info
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct LogLine {
    pub level: LogLevel,
    pub text: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum GameEvent {
    Log(LogLine),
    Exited {
        code: Option<i32>,
        crashed: bool,
        crash_report: Option<PathBuf>,
    },
}

/// Turns stdout lines into log entries; understands the log4j2 XML layout
/// used by the official logging configs.
#[derive(Default)]
pub struct LogParser {
    event: Option<XmlEvent>,
}

#[derive(Default)]
struct XmlEvent {
    level: Option<LogLevel>,
    thread: String,
    logger: String,
    message: String,
    in_message: bool,
}

fn attr<'a>(tag: &'a str, name: &str) -> Option<&'a str> {
    let pat = format!("{name}=\"");
    let start = tag.find(&pat)? + pat.len();
    let end = tag[start..].find('"')?;
    Some(&tag[start..start + end])
}

fn unescape(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

impl LogParser {
    pub fn push(&mut self, line: &str) -> Option<LogLine> {
        let trimmed = line.trim_start();
        if self.event.is_none() {
            if trimmed.starts_with("<log4j:Event") {
                self.event = Some(XmlEvent {
                    level: attr(trimmed, "level").and_then(LogLevel::parse),
                    thread: attr(trimmed, "thread").map(unescape).unwrap_or_default(),
                    logger: attr(trimmed, "logger").unwrap_or_default().to_owned(),
                    ..Default::default()
                });
                if !trimmed.contains("</log4j:Event>") {
                    return None;
                }
            } else {
                return Some(LogLine {
                    level: LogLevel::sniff(line),
                    text: line.to_owned(),
                });
            }
        }

        let event = self.event.as_mut()?;
        // Continuation lines keep their indentation (stack traces use tabs).
        let mut src = line;
        if !event.in_message {
            for open in ["<log4j:Message><![CDATA[", "<log4j:Throwable><![CDATA["] {
                if let Some(i) = trimmed.find(open) {
                    src = &trimmed[i + open.len()..];
                    event.in_message = true;
                    if !event.message.is_empty() && !event.message.ends_with('\n') {
                        event.message.push('\n');
                    }
                    break;
                }
            }
        }
        if event.in_message {
            match src.find("]]>") {
                Some(i) => {
                    event.message.push_str(&src[..i]);
                    event.in_message = false;
                }
                None => {
                    event.message.push_str(src);
                    event.message.push('\n');
                }
            }
        }
        if trimmed.contains("</log4j:Event>") {
            let e = self.event.take()?;
            let logger = e.logger.rsplit('.').next().unwrap_or_default();
            let level = e.level.unwrap_or(LogLevel::Info);
            let text = format!(
                "[{}/{:?}] {}{}",
                e.thread,
                level,
                if logger.is_empty() {
                    String::new()
                } else {
                    format!("[{logger}] ")
                },
                e.message.trim_end()
            );
            return Some(LogLine { level, text });
        }
        None
    }
}

/// A running game.
pub struct GameProcess {
    pub pid: Option<u32>,
    pub events: mpsc::UnboundedReceiver<GameEvent>,
    kill: Option<tokio::sync::oneshot::Sender<()>>,
}

impl GameProcess {
    pub fn kill(&mut self) {
        if let Some(tx) = self.kill.take() {
            let _ = tx.send(());
        }
    }
}

/// Spawn the game in `game_dir` and stream its output.
pub fn spawn(cmd: &Command, game_dir: &Path) -> Result<GameProcess> {
    std::fs::create_dir_all(game_dir).map_err(Error::io_at(game_dir))?;
    let mut command = tokio::process::Command::new(&cmd.program);
    command
        .args(&cmd.args)
        .current_dir(game_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(false);
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let started = SystemTime::now();
    let mut child: Child = command
        .spawn()
        .map_err(|e| Error::Java(format!("failed to start {}: {e}", cmd.program.display())))?;
    let pid = child.id();
    let (tx, rx) = mpsc::unbounded_channel();
    let (kill_tx, kill_rx) = tokio::sync::oneshot::channel::<()>();

    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let out_tx = tx.clone();
    let out_task = tokio::spawn(async move {
        let mut parser = LogParser::default();
        let mut lines = BufReader::new(stdout).lines();
        let mut saw_crash = false;
        while let Ok(Some(line)) = lines.next_line().await {
            if line.contains("#@!@# Game crashed!") || line.contains("---- Minecraft Crash Report ----") {
                saw_crash = true;
            }
            if let Some(entry) = parser.push(&line) {
                let _ = out_tx.send(GameEvent::Log(entry));
            }
        }
        saw_crash
    });
    let err_tx = tx.clone();
    let err_task = tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let level = match LogLevel::sniff(&line) {
                LogLevel::Info => LogLevel::Warn,
                l => l,
            };
            let _ = err_tx.send(GameEvent::Log(LogLine { level, text: line }));
        }
    });

    let game_dir = game_dir.to_path_buf();
    tokio::spawn(async move {
        let status = tokio::select! {
            s = child.wait() => s.ok(),
            _ = kill_rx => {
                let _ = child.kill().await;
                child.wait().await.ok()
            }
        };
        let saw_crash = out_task.await.unwrap_or(false);
        let _ = err_task.await;
        let code = status.and_then(|s| s.code());
        let crash_report = newest_crash_report(&game_dir, started);
        let crashed = saw_crash || crash_report.is_some() || code.is_some_and(|c| c != 0);
        let _ = tx.send(GameEvent::Exited {
            code,
            crashed,
            crash_report,
        });
    });

    Ok(GameProcess {
        pid,
        events: rx,
        kill: Some(kill_tx),
    })
}

/// The newest crash report written after `since`, if any.
pub fn newest_crash_report(game_dir: &Path, since: SystemTime) -> Option<PathBuf> {
    std::fs::read_dir(game_dir.join("crash-reports"))
        .ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let modified = e.metadata().ok()?.modified().ok()?;
            (modified >= since).then(|| (modified, e.path()))
        })
        .max_by_key(|(m, _)| *m)
        .map(|(_, p)| p)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_lines() {
        let mut p = LogParser::default();
        let l = p.push("[12:00:00] [Render thread/WARN]: Something odd").unwrap();
        assert_eq!(l.level, LogLevel::Warn);
        assert_eq!(p.push("hello").unwrap().level, LogLevel::Info);
        assert_eq!(p.push("\tat net.minecraft.Foo").unwrap().level, LogLevel::Error);
    }

    #[test]
    fn xml_events() {
        let mut p = LogParser::default();
        let lines = [
            r#"<log4j:Event logger="net.minecraft.client.Minecraft" timestamp="1" level="ERROR" thread="Render thread">"#,
            r#"  <log4j:Message><![CDATA[Setting user: Steve]]></log4j:Message>"#,
            r#"</log4j:Event>"#,
        ];
        assert!(p.push(lines[0]).is_none());
        assert!(p.push(lines[1]).is_none());
        let l = p.push(lines[2]).unwrap();
        assert_eq!(l.level, LogLevel::Error);
        assert_eq!(l.text, "[Render thread/Error] [Minecraft] Setting user: Steve");
    }

    #[test]
    fn xml_multiline_message() {
        let mut p = LogParser::default();
        p.push(r#"<log4j:Event logger="x" level="INFO" thread="main">"#);
        p.push(r#"<log4j:Message><![CDATA[line one"#);
        p.push("line two]]></log4j:Message>");
        let l = p.push("</log4j:Event>").unwrap();
        assert!(l.text.ends_with("line one\nline two"), "{}", l.text);
    }

    #[test]
    fn xml_throwable() {
        let mut p = LogParser::default();
        for line in [
            r#"<log4j:Event logger="a.B" level="ERROR" thread="main">"#,
            r#"  <log4j:Message><![CDATA[Failed]]></log4j:Message>"#,
            r#"  <log4j:Throwable><![CDATA[java.io.IOException: boom"#,
            "\tat a.B.c(B.java:1)",
            "]]></log4j:Throwable>",
        ] {
            assert!(p.push(line).is_none());
        }
        let l = p.push("</log4j:Event>").unwrap();
        assert_eq!(
            l.text,
            "[main/Error] [B] Failed\njava.io.IOException: boom\n\tat a.B.c(B.java:1)"
        );
    }
}
