#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "upstream").resolve()
CLI_UTIL = ROOT / "crates/biliup-cli/src/server/common/util.rs"
CORE_UTIL = ROOT / "crates/biliup/src/downloader/util.rs"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one upstream block, found {count}")
    return text.replace(old, new, 1)


def patch_cli() -> None:
    text = CLI_UTIL.read_text(encoding="utf-8")

    old = '''    /// 生成文件名模板（包含时间格式占位符），并清洗非法字符
    pub fn filename_template(&self) -> String {
        let raw = if let Some(prefix) = &self.filename_prefix {
            self.template_with(prefix)
        } else {
            format!("{}%Y-%m-%dT%H_%M_%S", self.streamer_info.name)
        };
        sanitize_filename(&raw)
    }

    fn template_with(&self, template: &str) -> String {
        template
            .replace("{streamer}", &self.streamer_info.name)
            .replace("{title}", &self.streamer_info.title)
            .replace("{url}", &self.streamer_info.url)
    }
'''
    new = '''    /// 生成文件名模板（包含时间格式占位符），并清洗非法字符
    pub fn filename_template(&self) -> String {
        if let Some(prefix) = &self.filename_prefix
            && is_recording_path_template(prefix)
        {
            let raw = self.template_with_sanitized_values(prefix);
            return sanitize_path_template(&raw);
        }

        let raw = if let Some(prefix) = &self.filename_prefix {
            self.template_with(prefix)
        } else {
            format!("{}%Y-%m-%dT%H_%M_%S", self.streamer_info.name)
        };
        sanitize_filename(&raw)
    }

    fn template_with(&self, template: &str) -> String {
        template
            .replace("{streamer}", &self.streamer_info.name)
            .replace("{title}", &self.streamer_info.title)
            .replace("{url}", &self.streamer_info.url)
    }

    fn template_with_sanitized_values(&self, template: &str) -> String {
        template
            .replace("{streamer}", &sanitize_filename(&self.streamer_info.name))
            .replace("{title}", &sanitize_filename(&self.streamer_info.title))
            .replace("{url}", &sanitize_filename(&self.streamer_info.url))
    }
'''
    text = replace_once(text, old, new, "Recorder filename template")

    old = '''    /// 生成“基名”（不带扩展名），时间冲突时按秒+1继续尝试，直到唯一
    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        let mut t = Local::now();

        loop {
            let base = t.format(&template).to_string();
            if !self.exists_with_suffix(&base, suffix) {
                return base;
            }
            t += Duration::seconds(1);
        }
    }

    /// 生成“基名”（不带扩展名）
    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&template)
            .to_string()
    }

    pub fn format(&self, template: &str) -> String {
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&self.template_with(template))
            .to_string()
    }
'''
    new = '''    /// 生成“基名”（不带扩展名），时间冲突时按秒+1继续尝试，直到唯一
    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        let mut t = Local::now();

        loop {
            let base = render_time_template(&template, t.naive_local());
            if !self.exists_with_suffix(&base, suffix) {
                return base;
            }
            t += Duration::seconds(1);
        }
    }

    /// 生成“基名”（不带扩展名）
    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        let local = self.streamer_info.date.with_timezone(&Local);
        render_time_template(&template, local.naive_local())
    }

    pub fn format(&self, template: &str) -> String {
        let local = self.streamer_info.date.with_timezone(&Local);
        render_time_template(&self.template_with(template), local.naive_local())
    }
'''
    text = replace_once(text, old, new, "Recorder time rendering")

    old = '''fn sanitize_filename(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    for ch in name.chars() {
        match ch {
            '\\\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => out.push('_'),
            c if c.is_control() => out.push('_'),
            _ => out.push(ch),
        }
    }
    let out = out.trim_end_matches([' ', '.']).to_string();
    if out.is_empty() { "_".to_string() } else { out }
}
'''
    new = old + '''
fn is_recording_path_template(template: &str) -> bool {
    template.starts_with("/recordings/") && template.contains("{record_date}")
}

fn sanitize_path_template(template: &str) -> String {
    let components = template
        .split('/')
        .filter(|component| !component.is_empty())
        .map(sanitize_filename)
        .collect::<Vec<_>>();
    format!("/{}", components.join("/"))
}

pub(crate) fn logical_record_date_naive(dt: chrono::NaiveDateTime) -> String {
    (dt - Duration::hours(4)).format("%Y-%m-%d").to_string()
}

fn render_time_template(template: &str, local: chrono::NaiveDateTime) -> String {
    let record_date = logical_record_date_naive(local);
    let expanded = template.replace("{record_date}", &record_date);
    local.format(&expanded).to_string()
}
'''
    text = replace_once(text, old, new, "path sanitization helpers")

    old = '''#[cfg(test)]
mod tests {
    use crate::server::common::util::media_ext_from_url;

    #[test]
    fn it_works() {
'''
    new = '''#[cfg(test)]
mod tests {
    use super::{logical_record_date_naive, media_ext_from_url, Recorder};
    use crate::server::infrastructure::models::StreamerInfo;
    use chrono::{NaiveDate, Utc};

    fn test_streamer_info(name: &str, title: &str) -> StreamerInfo {
        StreamerInfo::new(name, "https://example.com/live", title, Utc::now(), "")
    }

    #[test]
    fn legacy_template_keeps_flat_sanitization() {
        let info = test_streamer_info("主播/甲", "标题/一");
        let recorder = Recorder::new(Some("{streamer}/[%Y-%m-%d][{title}]".to_string()), info);
        assert_eq!(recorder.filename_template(), "主播_甲_[%Y-%m-%d][标题_一]");
    }

    #[test]
    fn recording_template_preserves_declared_directories_only() {
        let info = test_streamer_info("主播/甲", "标题:一/二?");
        let recorder = Recorder::new(
            Some("/recordings/{streamer}/{record_date}/[%Y-%m-%d][{title}]".to_string()),
            info,
        );
        assert_eq!(
            recorder.filename_template(),
            "/recordings/主播_甲/{record_date}/[%Y-%m-%d][标题_一_二_]"
        );
    }

    #[test]
    fn recording_template_neutralizes_dot_components() {
        let info = test_streamer_info("..", ".");
        let recorder = Recorder::new(
            Some("/recordings/{streamer}/{record_date}/{title}".to_string()),
            info,
        );
        assert_eq!(recorder.filename_template(), "/recordings/_/{record_date}/_");
    }

    #[test]
    fn non_recordings_absolute_template_stays_in_legacy_flat_mode() {
        let info = test_streamer_info("主播", "标题");
        let recorder = Recorder::new(
            Some("/tmp/{streamer}/{record_date}/{title}".to_string()),
            info,
        );
        assert_eq!(recorder.filename_template(), "_tmp_主播_{record_date}_标题");
    }

    #[test]
    fn logical_day_changes_at_four_am() {
        let before = NaiveDate::from_ymd_opt(2026, 8, 25)
            .unwrap()
            .and_hms_opt(3, 59, 59)
            .unwrap();
        let at_boundary = NaiveDate::from_ymd_opt(2026, 8, 25)
            .unwrap()
            .and_hms_opt(4, 0, 0)
            .unwrap();
        assert_eq!(logical_record_date_naive(before), "2026-08-24");
        assert_eq!(logical_record_date_naive(at_boundary), "2026-08-25");
    }

    #[test]
    fn recorder_time_render_keeps_real_clock_and_logical_day() {
        let dt = NaiveDate::from_ymd_opt(2026, 8, 25)
            .unwrap()
            .and_hms_opt(2, 10, 0)
            .unwrap();
        assert_eq!(
            super::render_time_template(
                "/recordings/主播/{record_date}/[%Y年%m月%d日-%H时%M分%S秒]",
                dt,
            ),
            "/recordings/主播/2026-08-24/[2026年08月25日-02时10分00秒]"
        );
    }

    #[test]
    fn it_works() {
'''
    text = replace_once(text, old, new, "biliup-cli tests")

    CLI_UTIL.write_text(text, encoding="utf-8")


def patch_core() -> None:
    text = CORE_UTIL.read_text(encoding="utf-8")

    text = replace_once(
        text,
        'use chrono::{DateTime, Local};\n',
        'use chrono::{Duration as ChronoDuration, Local, NaiveDateTime};\n',
        "core chrono imports",
    )

    old = '''pub fn format_filename(file_name: &str) -> String {
    let local: DateTime<Local> = Local::now();
    // let time_str = local.format("%Y-%m-%dT%H_%M_%S");
    let time_str = local.format(file_name);
    // format!("{file_name}{time_str}")
    time_str.to_string()
}
'''
    new = '''pub(crate) fn format_filename_at(file_name: &str, local: NaiveDateTime) -> String {
    let record_date = (local - ChronoDuration::hours(4))
        .format("%Y-%m-%d")
        .to_string();
    let template = file_name.replace("{record_date}", &record_date);
    local.format(&template).to_string()
}

pub fn format_filename(file_name: &str) -> String {
    format_filename_at(file_name, Local::now().naive_local())
}
'''
    text = replace_once(text, old, new, "stream-gears filename rendering")

    old = '''#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    #[test]
    fn it_works() -> Result<(), Box<dyn std::error::Error>> {
'''
    new = '''#[cfg(test)]
mod tests {
    use super::*;
    use std::path::{Path, PathBuf};

    #[test]
    fn format_filename_at_keeps_real_time_but_shifts_record_date() {
        let dt = chrono::NaiveDate::from_ymd_opt(2026, 8, 25)
            .unwrap()
            .and_hms_opt(2, 10, 0)
            .unwrap();
        assert_eq!(
            format_filename_at(
                "/recordings/主播/{record_date}/[%Y年%m月%d日-%H时%M分%S秒]",
                dt,
            ),
            "/recordings/主播/2026-08-24/[2026年08月25日-02时10分00秒]"
        );
    }

    #[test]
    fn lifecycle_file_creates_nested_parent_directory() -> Result<(), Box<dyn std::error::Error>> {
        let root = std::env::temp_dir().join(format!("biliup-path-test-{}", std::process::id()));
        let template = root
            .join("主播")
            .join("{record_date}")
            .join("[%Y年%m月%d日-%H时%M分%S秒]");
        let mut file = LifecycleFile::new(&template.to_string_lossy(), "flv");
        let path = file.create()?.to_path_buf();
        assert!(path.parent().is_some_and(Path::is_dir));
        std::fs::remove_dir_all(root)?;
        Ok(())
    }

    #[test]
    fn it_works() -> Result<(), Box<dyn std::error::Error>> {
'''
    text = replace_once(text, old, new, "biliup core tests")

    CORE_UTIL.write_text(text, encoding="utf-8")


def main() -> None:
    if not CLI_UTIL.is_file() or not CORE_UTIL.is_file():
        raise RuntimeError(f"upstream source tree not found under {ROOT}")
    patch_cli()
    patch_core()
    print("biliup-custom recording path patch applied successfully")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"biliup-custom patch failed: {exc}", file=sys.stderr)
        raise
