//! Current-user Windows logon registration; independent of Agent configuration.

use serde::Serialize;

const BACKGROUND_FLAG: &str = "--autostart";

#[derive(Serialize)]
pub struct StartupStatus {
    supported: bool,
    enabled: bool,
}

pub fn background_launch(arguments: impl IntoIterator<Item = impl AsRef<str>>) -> bool {
    arguments
        .into_iter()
        .any(|argument| argument.as_ref() == BACKGROUND_FLAG)
}

fn supported() -> bool {
    cfg!(target_os = "windows")
        && std::env::var_os("INFRA_SENTINEL_STATIC_PROJECTION").is_none()
        && std::env::var_os(crate::app_paths::STATE_DIRECTORY_ENV).is_none()
}

#[tauri::command]
pub fn desktop_startup_get() -> Result<StartupStatus, String> {
    let supported = supported();
    let mut enabled = false;
    #[cfg(target_os = "windows")]
    if supported {
        enabled = windows::is_enabled()?;
    }
    Ok(StartupStatus { supported, enabled })
}

#[tauri::command]
pub fn desktop_startup_set(enabled: bool) -> Result<StartupStatus, String> {
    if !supported() {
        return Err(
            "Automatic startup is available in the regular Windows desktop runtime.".into(),
        );
    }
    #[cfg(target_os = "windows")]
    windows::set_enabled(enabled)?;
    desktop_startup_get()
}

#[cfg(target_os = "windows")]
mod windows {
    use std::io::{self, ErrorKind};
    use std::path::Path;
    use winreg::enums::{HKEY_CURRENT_USER, KEY_QUERY_VALUE, KEY_SET_VALUE};
    use winreg::RegKey;

    // Tauri's NSIS uninstaller removes this product-name value on uninstall
    // and preserves it during updates. Keep its name aligned with productName.
    const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    const VALUE_NAME: &str = "Infra Sentinel";

    fn startup_command(executable: &Path) -> Result<String, String> {
        let path = executable
            .to_str()
            .ok_or("Executable path is not valid Unicode.")?;
        let command = format!("\"{path}\" {}", super::BACKGROUND_FLAG);
        if !executable.is_absolute()
            || path.contains(['\0', '"', '\r', '\n'])
            || command.encode_utf16().count() > 260
        {
            return Err("Executable path cannot be registered for Windows startup.".into());
        }
        Ok(command)
    }

    fn registered(key: &RegKey, command: &str) -> io::Result<bool> {
        match key.get_value::<String, _>(VALUE_NAME) {
            Ok(value) => Ok(value == command),
            Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error),
        }
    }

    fn update(key: &RegKey, command: &str, enabled: bool) -> io::Result<()> {
        if enabled {
            key.set_value(VALUE_NAME, &command)
        } else {
            match key.delete_value(VALUE_NAME) {
                Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
                result => result,
            }
        }
    }

    pub fn is_enabled() -> Result<bool, String> {
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let command = startup_command(&executable)?;
        match RegKey::predef(HKEY_CURRENT_USER).open_subkey_with_flags(RUN_KEY, KEY_QUERY_VALUE) {
            Ok(key) => registered(&key, &command).map_err(|error| error.to_string()),
            Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error.to_string()),
        }
    }

    pub fn set_enabled(enabled: bool) -> Result<(), String> {
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let command = startup_command(&executable)?;
        let user = RegKey::predef(HKEY_CURRENT_USER);
        let (key, _) = user
            .create_subkey_with_flags(RUN_KEY, KEY_SET_VALUE)
            .map_err(|error| error.to_string())?;
        update(&key, &command, enabled).map_err(|error| error.to_string())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn registry_registration_is_idempotent_and_preserves_other_entries() {
            // Test the real registry implementation without touching the Run key.
            let user = RegKey::predef(HKEY_CURRENT_USER);
            let path = format!(
                r"Software\Infra Sentinel\StartupTests\{}",
                uuid::Uuid::new_v4()
            );
            let (key, _) = user.create_subkey(&path).expect("private test key");
            let command = startup_command(Path::new(r"C:\Users\测试\Infra Sentinel\app.exe"))
                .expect("quoted Unicode path");
            assert_eq!(
                command,
                "\"C:\\Users\\测试\\Infra Sentinel\\app.exe\" --autostart"
            );
            key.set_value("another-app", &"preserve").unwrap();
            assert!(!registered(&key, &command).unwrap());
            update(&key, &command, true).unwrap();
            update(&key, &command, true).unwrap();
            assert!(registered(&key, &command).unwrap());
            assert!(!registered(&key, "another executable").unwrap());
            update(&key, &command, false).unwrap();
            update(&key, &command, false).unwrap();
            assert!(!registered(&key, &command).unwrap());
            assert_eq!(
                key.get_value::<String, _>("another-app").unwrap(),
                "preserve"
            );
            drop(key);
            user.delete_subkey_all(path).unwrap();
        }

        #[test]
        fn invalid_startup_paths_are_rejected_before_registration() {
            for path in ["relative.exe", "C:\\bad\"path.exe", "C:\\bad\0path.exe"] {
                assert!(startup_command(Path::new(path)).is_err());
            }
            assert!(startup_command(Path::new(&format!("C:\\{}.exe", "a".repeat(260)))).is_err());
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn only_the_explicit_logon_flag_requests_background_startup() {
        assert!(super::background_launch(["app.exe", "--autostart"]));
        assert!(!super::background_launch(["app.exe", "--autostart=false"]));
        assert!(!super::background_launch(["app.exe"]));
    }
}
