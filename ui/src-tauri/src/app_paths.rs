//! App-owned filesystem locations.
//!
//! Both the native bridge and the Agent supervisor need these locations, but
//! neither should infer them independently. Keeping the convention here also
//! lets a test or a power user point the whole runtime at a private state
//! directory without granting the WebView arbitrary filesystem access.

use std::env;
use std::path::PathBuf;

pub const STATE_DIRECTORY_ENV: &str = "INFRA_SENTINEL_STATE_DIR";

pub fn support_dir() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os(STATE_DIRECTORY_ENV) {
        return PathBuf::from(configured)
            .parent()
            .map(PathBuf::from)
            .ok_or_else(|| "state directory has no parent support directory".to_owned());
    }
    dirs::data_dir()
        .map(|directory| directory.join("Infra Sentinel"))
        .ok_or_else(|| "cannot determine an application data directory".to_owned())
}

pub fn state_dir() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os(STATE_DIRECTORY_ENV) {
        return Ok(PathBuf::from(configured));
    }
    Ok(support_dir()?.join("state"))
}

pub fn config_path() -> Result<PathBuf, String> {
    Ok(support_dir()?.join("config.toml"))
}

#[cfg(test)]
mod tests {
    use super::{config_path, state_dir, STATE_DIRECTORY_ENV};

    #[test]
    fn configured_state_directory_keeps_config_beside_state() {
        let previous = std::env::var_os(STATE_DIRECTORY_ENV);
        let temporary =
            std::env::temp_dir().join(format!("infra-sentinel-paths-{}", uuid::Uuid::new_v4()));
        let configured = temporary.join("state");
        std::env::set_var(STATE_DIRECTORY_ENV, &configured);
        assert_eq!(state_dir().expect("state path"), configured);
        assert_eq!(
            config_path().expect("config path"),
            temporary.join("config.toml")
        );
        match previous {
            Some(value) => std::env::set_var(STATE_DIRECTORY_ENV, value),
            None => std::env::remove_var(STATE_DIRECTORY_ENV),
        }
    }
}
