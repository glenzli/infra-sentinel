//! App-owned filesystem locations.
//!
//! Both the native bridge and the Agent supervisor need these locations, but
//! neither should infer them independently. Keeping the convention here also
//! lets a test or a power user point the whole runtime at a private state
//! directory without granting the WebView arbitrary filesystem access.

use std::env;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

pub const STATE_DIRECTORY_ENV: &str = "INFRA_SENTINEL_STATE_DIR";
const SUPPORT_DIRECTORY_NAME: &str = "Infra Sentinel";
const LEGACY_SUPPORT_DIRECTORY_NAME: &str = "Traffic Sentinel";
const CONFIG_FILE_NAME: &str = "config.toml";

pub fn support_dir() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os(STATE_DIRECTORY_ENV) {
        return PathBuf::from(configured)
            .parent()
            .map(PathBuf::from)
            .ok_or_else(|| "state directory has no parent support directory".to_owned());
    }
    dirs::data_dir()
        .map(|directory| directory.join(SUPPORT_DIRECTORY_NAME))
        .ok_or_else(|| "cannot determine an application data directory".to_owned())
}

pub fn state_dir() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os(STATE_DIRECTORY_ENV) {
        return Ok(PathBuf::from(configured));
    }
    Ok(support_dir()?.join("state"))
}

pub fn config_path() -> Result<PathBuf, String> {
    Ok(support_dir()?.join(CONFIG_FILE_NAME))
}

fn migrate_legacy_config_between(legacy_support: &Path, support: &Path) -> Result<bool, String> {
    let legacy_config = legacy_support.join(CONFIG_FILE_NAME);
    let config = support.join(CONFIG_FILE_NAME);

    match fs::symlink_metadata(&config) {
        Ok(_) => return Ok(false),
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!(
                "cannot inspect current Infra Sentinel config: {error}"
            ))
        }
    }
    let legacy_metadata = match fs::symlink_metadata(&legacy_config) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(error) => {
            return Err(format!(
                "cannot inspect legacy Traffic Sentinel config: {error}"
            ))
        }
    };
    if !legacy_metadata.file_type().is_file() {
        return Err("legacy Traffic Sentinel config is not a regular file".to_owned());
    }

    fs::create_dir_all(support)
        .map_err(|error| format!("cannot create Infra Sentinel support directory: {error}"))?;
    let staging = support.join(format!(".config.toml.migrating-{}", uuid::Uuid::new_v4()));
    if let Err(error) = fs::copy(&legacy_config, &staging) {
        let _ = fs::remove_file(&staging);
        return Err(format!(
            "cannot stage legacy Traffic Sentinel config: {error}"
        ));
    }

    let linked = match fs::hard_link(&staging, &config) {
        Ok(()) => true,
        Err(error) if error.kind() == ErrorKind::AlreadyExists => false,
        Err(error) => {
            let _ = fs::remove_file(&staging);
            return Err(format!(
                "cannot install migrated Infra Sentinel config: {error}"
            ));
        }
    };
    fs::remove_file(&staging)
        .map_err(|error| format!("cannot remove migrated config staging file: {error}"))?;
    Ok(linked)
}

/// Copy the v1 Traffic Sentinel configuration into the renamed support
/// directory without overwriting either side. Historical v1 samples remain in
/// the legacy directory because the v2 metric store has a different contract.
pub fn migrate_legacy_configuration() -> Result<bool, String> {
    if env::var_os(STATE_DIRECTORY_ENV).is_some() {
        return Ok(false);
    }
    let data = dirs::data_dir()
        .ok_or_else(|| "cannot determine an application data directory".to_owned())?;
    migrate_legacy_config_between(
        &data.join(LEGACY_SUPPORT_DIRECTORY_NAME),
        &data.join(SUPPORT_DIRECTORY_NAME),
    )
}

#[cfg(test)]
mod tests {
    use super::{config_path, migrate_legacy_config_between, state_dir, STATE_DIRECTORY_ENV};
    use std::fs;

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

    #[test]
    fn legacy_configuration_is_copied_without_removing_the_source() {
        let root = std::env::temp_dir().join(format!(
            "infra-sentinel-legacy-config-{}",
            uuid::Uuid::new_v4()
        ));
        let legacy = root.join("Traffic Sentinel");
        let current = root.join("Infra Sentinel");
        fs::create_dir_all(&legacy).expect("create legacy support directory");
        fs::write(legacy.join("config.toml"), b"schema = 'legacy'\n").expect("write legacy config");

        assert!(migrate_legacy_config_between(&legacy, &current).expect("migrate config"));
        assert_eq!(
            fs::read(current.join("config.toml")).expect("read migrated config"),
            b"schema = 'legacy'\n"
        );
        assert!(legacy.join("config.toml").is_file());

        fs::remove_dir_all(root).expect("remove private test directory");
    }

    #[test]
    fn legacy_configuration_never_overwrites_current_configuration() {
        let root = std::env::temp_dir().join(format!(
            "infra-sentinel-current-config-{}",
            uuid::Uuid::new_v4()
        ));
        let legacy = root.join("Traffic Sentinel");
        let current = root.join("Infra Sentinel");
        fs::create_dir_all(&legacy).expect("create legacy support directory");
        fs::create_dir_all(&current).expect("create current support directory");
        fs::write(legacy.join("config.toml"), b"schema = 'legacy'\n").expect("write legacy config");
        fs::write(current.join("config.toml"), b"schema = 'current'\n")
            .expect("write current config");

        assert!(!migrate_legacy_config_between(&legacy, &current).expect("skip migration"));
        assert_eq!(
            fs::read(current.join("config.toml")).expect("read current config"),
            b"schema = 'current'\n"
        );

        fs::remove_dir_all(root).expect("remove private test directory");
    }
}
