//! Maven coordinates (`group:artifact:version[:classifier][@ext]`).

use std::fmt;
use std::str::FromStr;

use crate::error::Error;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MavenCoord {
    pub group: String,
    pub artifact: String,
    pub version: String,
    pub classifier: Option<String>,
    pub extension: String,
}

impl MavenCoord {
    /// Relative repository path, always with `/` separators.
    pub fn path(&self) -> String {
        let classifier = self
            .classifier
            .as_deref()
            .map(|c| format!("-{c}"))
            .unwrap_or_default();
        format!(
            "{}/{}/{}/{}-{}{}.{}",
            self.group.replace('.', "/"),
            self.artifact,
            self.version,
            self.artifact,
            self.version,
            classifier,
            self.extension
        )
    }

    /// Identity used to de-duplicate libraries: everything but the version.
    pub fn key(&self) -> String {
        match &self.classifier {
            Some(c) => format!("{}:{}:{}", self.group, self.artifact, c),
            None => format!("{}:{}", self.group, self.artifact),
        }
    }

    pub fn with_classifier(&self, classifier: &str) -> Self {
        Self {
            classifier: Some(classifier.to_owned()),
            ..self.clone()
        }
    }
}

impl FromStr for MavenCoord {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let (coords, extension) = match s.split_once('@') {
            Some((c, e)) => (c, e.to_owned()),
            None => (s, "jar".to_owned()),
        };
        let parts: Vec<&str> = coords.split(':').collect();
        let bad = || Error::InvalidCoordinate(s.to_owned());
        if parts.len() < 3 || parts.len() > 4 || parts.iter().any(|p| p.is_empty()) {
            return Err(bad());
        }
        Ok(Self {
            group: parts[0].to_owned(),
            artifact: parts[1].to_owned(),
            version: parts[2].to_owned(),
            classifier: parts.get(3).map(|c| (*c).to_owned()),
            extension,
        })
    }
}

impl fmt::Display for MavenCoord {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}:{}", self.group, self.artifact, self.version)?;
        if let Some(c) = &self.classifier {
            write!(f, ":{c}")?;
        }
        if self.extension != "jar" {
            write!(f, "@{}", self.extension)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_plain_coordinate() {
        let c: MavenCoord = "org.ow2.asm:asm:9.6".parse().unwrap();
        assert_eq!(c.path(), "org/ow2/asm/asm/9.6/asm-9.6.jar");
        assert_eq!(c.key(), "org.ow2.asm:asm");
    }

    #[test]
    fn parses_classifier_and_extension() {
        let c: MavenCoord = "net.minecraftforge:forge:1.20.1-47.2.0:client@zip"
            .parse()
            .unwrap();
        assert_eq!(
            c.path(),
            "net/minecraftforge/forge/1.20.1-47.2.0/forge-1.20.1-47.2.0-client.zip"
        );
        assert_eq!(c.to_string(), "net.minecraftforge:forge:1.20.1-47.2.0:client@zip");
    }

    #[test]
    fn rejects_garbage() {
        assert!("nope".parse::<MavenCoord>().is_err());
        assert!("a::b".parse::<MavenCoord>().is_err());
    }
}
