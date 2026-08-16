//! Lifecycle owner for the packaged Infra Agent sidecar.
//!
//! The Agent remains responsible for collection, storage, policy evaluation,
//! notifications, and Projection construction. This module only provides the
//! desktop lifetime around that already-versioned contract: bootstrap the
//! local config/state directories, launch the packaged executable, and relaunch
//! it after a configuration command asks the Agent to exit cleanly.

use crate::app_paths::{config_path, state_dir, support_dir, STATE_DIRECTORY_ENV};
use crate::projection_cache::ProjectionCache;
use std::ffi::OsString;
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Manager};

const RESTART_DELAY: Duration = Duration::from_secs(2);
const PROJECTION_STREAM_ENV: &str = "INFRA_SENTINEL_PROJECTION_STREAM";
const STATIC_PROJECTION_ENV: &str = "INFRA_SENTINEL_STATIC_PROJECTION";
const STATIC_DASHBOARD_ENV: &str = "INFRA_SENTINEL_STATIC_SHOW_DASHBOARD";
const STATIC_LOCALE_ENV: &str = "INFRA_SENTINEL_STATIC_DEMO_LOCALE";
const MAX_PROJECTION_FRAME_BYTES: usize = 2 * 1024 * 1024;

fn static_projection_path_from(value: Option<OsString>) -> Option<PathBuf> {
    value.filter(|value| !value.is_empty()).map(PathBuf::from)
}

fn static_projection_path() -> Option<PathBuf> {
    static_projection_path_from(std::env::var_os(STATIC_PROJECTION_ENV))
}

fn should_show_static_dashboard(value: Option<OsString>) -> bool {
    matches!(value.as_deref(), Some(value) if !value.is_empty() && value != "0")
}

fn static_demo_locale_from(value: Option<OsString>) -> Option<String> {
    let locale = value?.to_string_lossy().into_owned();
    matches!(locale.as_str(), "en" | "zh").then_some(locale)
}

pub fn static_demo_locale() -> Option<String> {
    static_demo_locale_from(std::env::var_os(STATIC_LOCALE_ENV))
}

fn sidecar_file_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "infra-agent.exe"
    } else {
        "infra-agent"
    }
}

fn packaged_agent_path() -> Result<PathBuf, String> {
    let executable = std::env::current_exe()
        .map_err(|error| format!("cannot locate Infra Sentinel executable: {error}"))?;
    executable
        .parent()
        .map(|directory| directory.join(sidecar_file_name()))
        .ok_or_else(|| "Infra Sentinel executable has no containing directory".to_owned())
}

fn development_config_template() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("config.example.toml")
}

fn bundled_config_template(app: &AppHandle) -> Result<PathBuf, String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| format!("cannot locate application resources: {error}"))?
        .join("config.example.toml");
    if bundled.is_file() {
        Ok(bundled)
    } else {
        let development = development_config_template();
        if development.is_file() {
            Ok(development)
        } else {
            Err("packaged config.example.toml is missing".to_owned())
        }
    }
}

fn bootstrap_runtime(app: &AppHandle) -> Result<(PathBuf, PathBuf, PathBuf), String> {
    let support = support_dir()?;
    let state = state_dir()?;
    let config = config_path()?;
    fs::create_dir_all(&state)
        .map_err(|error| format!("cannot create Agent state directory: {error}"))?;
    if !config.exists() {
        let template = bundled_config_template(app)?;
        fs::copy(&template, &config)
            .map_err(|error| format!("cannot create default Agent config: {error}"))?;
    }
    let agent = packaged_agent_path()?;
    if !agent.is_file() {
        return Err(format!(
            "packaged Infra Agent is missing: {}",
            agent.display()
        ));
    }
    Ok((support, state, config))
}

fn consume_projection_stream<R: Read>(reader: R, cache: ProjectionCache) -> Result<usize, String> {
    let mut accepted = 0;
    for line in BufReader::new(reader).lines() {
        let line = line.map_err(|error| format!("cannot read Agent Projection stream: {error}"))?;
        if line.is_empty() {
            continue;
        }
        if line.len() > MAX_PROJECTION_FRAME_BYTES {
            continue;
        }
        if cache.replace_document(&line).is_ok() {
            accepted += 1;
        }
    }
    Ok(accepted)
}

fn launch_once(
    agent: &Path,
    support: &Path,
    state: &Path,
    config: &Path,
    cache: ProjectionCache,
) -> Result<(), String> {
    let mut child = Command::new(agent)
        .arg("--config")
        .arg(config)
        .arg("--watch")
        .current_dir(support)
        .env(STATE_DIRECTORY_ENV, state)
        .env("INFRA_SENTINEL_PARENT_PID", std::process::id().to_string())
        .env("INFRA_SENTINEL_APP_NOTIFICATIONS", "1")
        .env(PROJECTION_STREAM_ENV, "stdio")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("cannot start Infra Agent: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Infra Agent Projection stream is unavailable".to_owned())?;
    let reader = thread::Builder::new()
        .name("infra-agent-projection-stream".to_owned())
        .spawn(move || consume_projection_stream(stdout, cache))
        .map_err(|error| format!("cannot start Agent Projection reader: {error}"))?;
    child
        .wait()
        .map_err(|error| format!("cannot wait for Infra Agent: {error}"))?;
    reader
        .join()
        .map_err(|_| "Agent Projection reader panicked".to_owned())??;
    Ok(())
}

pub fn start(app: AppHandle, cache: ProjectionCache) -> Result<(), String> {
    if let Some(path) = static_projection_path() {
        cache
            .load_checkpoint(&path)
            .map_err(|error| format!("cannot load static demo Projection: {error}"))?;
        if should_show_static_dashboard(std::env::var_os(STATIC_DASHBOARD_ENV)) {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        return Ok(());
    }
    let (support, state, config) = bootstrap_runtime(&app)?;
    let _ = cache.load_checkpoint(&state.join("projection.json"));
    let agent = packaged_agent_path()?;
    thread::Builder::new()
        .name("infra-agent-supervisor".to_owned())
        .spawn(move || loop {
            let _ = launch_once(&agent, &support, &state, &config, cache.clone());
            thread::sleep(RESTART_DELAY);
        })
        .map_err(|error| format!("cannot start Infra Agent supervisor: {error}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        consume_projection_stream, development_config_template, should_show_static_dashboard,
        sidecar_file_name, static_demo_locale_from, static_projection_path_from,
    };
    use crate::projection_cache::{ProjectionCache, PROTOCOL_SCHEMA};
    use std::ffi::OsString;
    use std::io::Cursor;

    #[test]
    fn packaged_agent_uses_a_platform_native_name() {
        #[cfg(target_os = "windows")]
        assert_eq!(sidecar_file_name(), "infra-agent.exe");
        #[cfg(not(target_os = "windows"))]
        assert_eq!(sidecar_file_name(), "infra-agent");
    }

    #[test]
    fn development_build_can_bootstrap_from_the_repository_template() {
        assert!(development_config_template().is_file());
    }

    #[test]
    fn projection_stream_keeps_the_latest_valid_frame() {
        let cache = ProjectionCache::default();
        let stream = format!(
            "{{\"schema\":\"{PROTOCOL_SCHEMA}\",\"epoch\":1}}\nnot-json\n{{\"schema\":\"{PROTOCOL_SCHEMA}\",\"epoch\":2}}\n"
        );

        let accepted = consume_projection_stream(Cursor::new(stream), cache.clone())
            .expect("consume projection stream");

        assert_eq!(accepted, 2);
        assert_eq!(
            cache.snapshot().and_then(|value| value["epoch"].as_i64()),
            Some(2)
        );
    }

    #[test]
    fn static_projection_path_is_opt_in_and_accepts_a_single_explicit_path() {
        assert_eq!(static_projection_path_from(None), None);
        assert_eq!(static_projection_path_from(Some(OsString::new())), None);
        assert_eq!(
            static_projection_path_from(Some(OsString::from("/tmp/infra-demo/projection.json"))),
            Some(std::path::PathBuf::from("/tmp/infra-demo/projection.json")),
        );
    }

    #[test]
    fn static_dashboard_requires_an_explicit_nonzero_environment_value() {
        assert!(!should_show_static_dashboard(None));
        assert!(!should_show_static_dashboard(Some(OsString::new())));
        assert!(!should_show_static_dashboard(Some(OsString::from("0"))));
        assert!(should_show_static_dashboard(Some(OsString::from("1"))));
    }

    #[test]
    fn static_demo_locale_accepts_only_the_two_documented_ui_values() {
        assert_eq!(static_demo_locale_from(None), None);
        assert_eq!(
            static_demo_locale_from(Some(OsString::from("en"))).as_deref(),
            Some("en")
        );
        assert_eq!(static_demo_locale_from(Some(OsString::from("de"))), None);
    }
}
