//! Human-friendly formatting for the UI.

use chrono::{DateTime, Utc};

pub fn relative_time(t: DateTime<Utc>, now: DateTime<Utc>) -> String {
    let secs = (now - t).num_seconds().max(0);
    match secs {
        0..=59 => "just now".into(),
        60..=3599 => plural(secs / 60, "minute"),
        3600..=86_399 => plural(secs / 3600, "hour"),
        86_400..=172_799 => "yesterday".into(),
        172_800..=2_591_999 => plural(secs / 86_400, "day"),
        _ => t.format("%b %-d, %Y").to_string(),
    }
}

fn plural(n: i64, unit: &str) -> String {
    if n == 1 {
        format!("1 {unit} ago")
    } else {
        format!("{n} {unit}s ago")
    }
}

pub fn megabytes(bytes: u64) -> String {
    let mb = bytes as f64 / 1_000_000.0;
    if mb >= 100.0 {
        format!("{mb:.0} MB")
    } else {
        format!("{mb:.1} MB")
    }
}

pub fn count(n: u64) -> String {
    match n {
        0..=999 => n.to_string(),
        1_000..=999_999 => trim(format!("{:.1}", n as f64 / 1e3)) + "K",
        1_000_000..=999_999_999 => trim(format!("{:.1}", n as f64 / 1e6)) + "M",
        _ => trim(format!("{:.1}", n as f64 / 1e9)) + "B",
    }
}

fn trim(s: String) -> String {
    s.strip_suffix(".0").map(str::to_owned).unwrap_or(s)
}

/// Thousands separators: 12345 → "12,345".
pub fn grouped(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

pub fn duration(secs: u64) -> String {
    let (h, m, s) = (secs / 3600, (secs / 60) % 60, secs % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

pub fn initial(name: &str) -> String {
    name.trim()
        .chars()
        .find(|c| c.is_alphanumeric())
        .map(|c| c.to_uppercase().collect())
        .unwrap_or_else(|| "V".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relative() {
        let now = Utc::now();
        assert_eq!(relative_time(now, now), "just now");
        assert_eq!(
            relative_time(now - chrono::Duration::minutes(1), now),
            "1 minute ago"
        );
        assert_eq!(
            relative_time(now - chrono::Duration::hours(5), now),
            "5 hours ago"
        );
        assert_eq!(relative_time(now - chrono::Duration::hours(30), now), "yesterday");
        assert_eq!(relative_time(now - chrono::Duration::days(3), now), "3 days ago");
    }

    #[test]
    fn numbers() {
        assert_eq!(count(999), "999");
        assert_eq!(count(1_000), "1K");
        assert_eq!(count(12_345), "12.3K");
        assert_eq!(count(4_200_000), "4.2M");
        assert_eq!(grouped(1_234_567), "1,234,567");
        assert_eq!(megabytes(18_300_000), "18.3 MB");
        assert_eq!(duration(3725), "1:02:05");
        assert_eq!(duration(65), "1:05");
        assert_eq!(initial("  my pack"), "M");
        assert_eq!(initial("!!"), "V");
    }
}
