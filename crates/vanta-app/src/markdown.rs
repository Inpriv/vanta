//! Turn a Modrinth project description (Markdown/HTML) into short plain text.

/// Plain-text summary of `md`, at most `max_chars` characters.
pub fn summary(md: &str, max_chars: usize) -> String {
    let mut out = String::new();
    let mut in_code = false;
    for raw in md.lines() {
        let line = raw.trim();
        if line.starts_with("```") {
            in_code = !in_code;
            continue;
        }
        if in_code
            || line.starts_with('<')
            || line.starts_with("![")
            || line.starts_with('|')
            || line.starts_with("---")
        {
            continue;
        }
        let line = line.trim_start_matches(['#', '>', ' ']);
        let line = line
            .strip_prefix("- ")
            .or_else(|| line.strip_prefix("* "))
            .map(|l| format!("• {l}"))
            .unwrap_or_else(|| line.to_owned());
        let text = strip_inline(&line);
        if text.trim().is_empty() {
            if !out.ends_with("\n\n") && !out.is_empty() {
                out.push_str("\n\n");
            }
            continue;
        }
        if !out.is_empty() && !out.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(text.trim());
        if out.chars().count() >= max_chars {
            break;
        }
    }
    let out = out.trim();
    if out.chars().count() > max_chars {
        let cut: String = out.chars().take(max_chars).collect();
        let cut = cut.rsplit_once(' ').map(|(a, _)| a).unwrap_or(&cut);
        format!("{cut}…")
    } else {
        out.to_owned()
    }
}

/// Remove links, images, emphasis and inline HTML from one line.
fn strip_inline(line: &str) -> String {
    let chars: Vec<char> = line.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        match c {
            // ![alt](url) → drop; [text](url) → text
            '!' if chars.get(i + 1) == Some(&'[') => {
                i = skip_link(&chars, i + 1).unwrap_or(i + 1);
                continue;
            }
            '[' => {
                if let Some(close) = chars[i..].iter().position(|&c| c == ']').map(|p| p + i) {
                    let text: String = chars[i + 1..close].iter().collect();
                    let end = if chars.get(close + 1) == Some(&'(') {
                        chars[close..]
                            .iter()
                            .position(|&c| c == ')')
                            .map(|p| p + close + 1)
                            .unwrap_or(close + 1)
                    } else {
                        close + 1
                    };
                    out.push_str(&strip_inline(&text));
                    i = end;
                    continue;
                }
            }
            '<' => {
                if let Some(close) = chars[i..].iter().position(|&c| c == '>') {
                    i += close + 1;
                    continue;
                }
            }
            '*' | '_' | '`' | '~' => {
                i += 1;
                continue;
            }
            _ => {}
        }
        out.push(c);
        i += 1;
    }
    out
}

fn skip_link(chars: &[char], open: usize) -> Option<usize> {
    let close = chars[open..].iter().position(|&c| c == ']')? + open;
    if chars.get(close + 1) == Some(&'(') {
        Some(chars[close..].iter().position(|&c| c == ')')? + close + 1)
    } else {
        Some(close + 1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_markdown() {
        let md = "# Sodium\n\n![banner](https://x/b.png)\n\nA **fast** [rendering](https://x) mod.\n\n<p align=center>hi</p>\n\n- Faster chunks\n- `Less` lag\n\n```\ncode\n```";
        assert_eq!(
            summary(md, 500),
            "Sodium\n\nA fast rendering mod.\n\n• Faster chunks\n• Less lag"
        );
    }

    #[test]
    fn truncates_on_a_word() {
        let s = summary("one two three four five", 12);
        assert_eq!(s, "one two…");
    }
}
