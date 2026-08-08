//! Lifecycle owner for the packaged Infra Agent sidecar.
//!
//! The Agent remains responsible for collection, storage, policy evaluation,
//! notifications, and Projection construction. This module only provides the
//! desktop lifetime around that already-versioned contract: bootstrap the
//! local config/state directories, launch the packaged executable, and relaunch
//! it after a configuration command asks the Agent to exit cleanly.

use crate::app_paths::{config_path, state_dir, support_dir, STATE_DIRECTORY_ENV};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Manager};

const RESTART_DELAY: Duration = Duration::from_secs(2);

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

fn launch_once(agent: &Path, support: &Path, state: &Path, config: &Path) -> Result<(), String> {
    let mut child = Command::new(agent)
        .arg("--config")
        .arg(config)
        .arg("--watch")
        .current_dir(support)
        .env(STATE_DIRECTORY_ENV, state)
        .env("INFRA_SENTINEL_PARENT_PID", std::process::id().to_string())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("cannot start Infra Agent: {error}"))?;
    child
        .wait()
        .map_err(|error| format!("cannot wait for Infra Agent: {error}"))?;
    Ok(())
}

pub fn start(app: AppHandle) -> Result<(), String> {
    let (support, state, config) = bootstrap_runtime(&app)?;
    let agent = packaged_agent_path()?;
    thread::Builder::new()
        .name("infra-agent-supervisor".to_owned())
        .spawn(move || loop {
            let _ = launch_once(&agent, &support, &state, &config);
            thread::sleep(RESTART_DELAY);
        })
        .map_err(|error| format!("cannot start Infra Agent supervisor: {error}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{development_config_template, sidecar_file_name};

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
}
