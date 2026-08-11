//! Narrow desktop-to-Agent bridge.
//!
//! The frontend cannot read arbitrary files or start arbitrary processes. It
//! can only consume the public Agent Projection and submit an allowlisted
//! command document. The Python Agent remains the sole owner of collection,
//! metric storage, policy evaluation, and projection construction.

use serde::Serialize;
use serde_json::{json, Map, Value};
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::State;
use uuid::Uuid;

use crate::app_paths::state_dir;
use crate::projection_cache::{ProjectionCache, PROTOCOL_SCHEMA};

// Keep this in lockstep with src/infra_sentinel/app/protocol.py. The desktop bridge owns
// command/projection validation, so a dated protocol revision must change on
// both sides before a new Agent can be displayed.
#[derive(Serialize)]
pub struct CommandReceipt {
    id: String,
}

fn command_allowed(command_type: &str) -> bool {
    matches!(
        command_type,
        "session.reset" | "metrics.query" | "configuration.get" | "configuration.update"
    )
}

fn requested_at() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("unix:{seconds}")
}

fn publish_command(
    commands: &Path,
    command_type: &str,
    payload: Map<String, Value>,
) -> Result<CommandReceipt, String> {
    if !command_allowed(command_type) {
        return Err("this desktop shell does not allow that Agent command".to_owned());
    }
    let command_id = Uuid::new_v4().to_string();
    fs::create_dir_all(commands)
        .map_err(|error| format!("cannot create command directory: {error}"))?;
    let document = json!({
        "schema": PROTOCOL_SCHEMA,
        "id": command_id,
        "type": command_type,
        "payload": payload,
        "requested_at": requested_at(),
    });
    let temporary = commands.join(format!(".{command_id}.tmp"));
    let request = commands.join(format!("{command_id}.request.json"));
    let encoded =
        serde_json::to_vec(&document).map_err(|error| format!("cannot encode command: {error}"))?;
    fs::write(&temporary, encoded).map_err(|error| format!("cannot write command: {error}"))?;
    fs::rename(&temporary, &request).map_err(|error| format!("cannot publish command: {error}"))?;
    Ok(CommandReceipt { id: command_id })
}

fn write_command(
    command_type: &str,
    payload: Map<String, Value>,
) -> Result<CommandReceipt, String> {
    publish_command(&state_dir()?.join("commands"), command_type, payload)
}

#[tauri::command]
pub fn read_projection(cache: State<'_, ProjectionCache>) -> Result<Option<Value>, String> {
    Ok(cache.snapshot().map(|projection| (*projection).clone()))
}

#[tauri::command]
pub fn read_agent_command_result(command_id: String) -> Result<Option<Value>, String> {
    let command_id = Uuid::parse_str(&command_id)
        .map_err(|_| "Agent command id is invalid".to_owned())?
        .to_string();
    let path = state_dir()?
        .join("commands")
        .join(format!("{command_id}.result.json"));
    decode_and_consume_result(&path, &command_id)
}

fn decode_and_consume_result(path: &Path, command_id: &str) -> Result<Option<Value>, String> {
    let document = match fs::read_to_string(path) {
        Ok(document) => document,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("cannot read Agent command result: {error}")),
    };
    let result: Value = serde_json::from_str(&document)
        .map_err(|error| format!("Agent command result is not valid JSON: {error}"))?;
    if result.get("schema").and_then(Value::as_str) != Some(PROTOCOL_SCHEMA)
        || result.get("id").and_then(Value::as_str) != Some(command_id)
    {
        return Err("Agent command result protocol is invalid".to_owned());
    }
    fs::remove_file(path)
        .map_err(|error| format!("cannot consume Agent command result: {error}"))?;
    Ok(Some(result))
}

#[tauri::command]
pub fn submit_agent_command(
    command_type: String,
    payload: Value,
) -> Result<CommandReceipt, String> {
    let object = payload
        .as_object()
        .cloned()
        .ok_or_else(|| "Agent command payload must be an object".to_owned())?;
    write_command(&command_type, object)
}

fn validated_console_url(value: &str) -> Result<url::Url, String> {
    let parsed = url::Url::parse(value).map_err(|_| "Console URL is invalid".to_owned())?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err("Console URL must use loopback HTTP(S)".to_owned());
    }
    let address = match parsed.host() {
        Some(url::Host::Ipv4(address)) => std::net::IpAddr::V4(address),
        Some(url::Host::Ipv6(address)) => std::net::IpAddr::V6(address),
        _ => return Err("Console URL must use a literal loopback address".to_owned()),
    };
    if !address.is_loopback() {
        return Err("Console URL must use a loopback address".to_owned());
    }
    Ok(parsed)
}

fn validated_external_status_url(value: &str) -> Result<url::Url, String> {
    let parsed = url::Url::parse(value).map_err(|_| "Status URL is invalid".to_owned())?;
    if parsed.scheme() != "https" || !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Status URL must use public HTTPS".to_owned());
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| "Status URL has no host".to_owned())?;
    if !matches!(
        host,
        "status.openai.com" | "status.claude.com" | "status.deepseek.com"
    ) {
        return Err("Status URL host is not allowlisted".to_owned());
    }
    Ok(parsed)
}

fn launch_url(url: url::Url) -> Result<(), String> {
    let url = url.to_string();
    #[cfg(target_os = "macos")]
    let mut command = Command::new("/usr/bin/open");
    #[cfg(target_os = "windows")]
    let mut command = {
        let mut command = Command::new("rundll32.exe");
        command.arg("url.dll,FileProtocolHandler");
        command
    };
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let mut command = Command::new("xdg-open");
    let status = command
        .arg(url)
        .status()
        .map_err(|error| format!("cannot open URL: {error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err("the platform URL opener rejected the URL".to_owned())
    }
}

#[tauri::command]
pub fn open_console(url: String) -> Result<(), String> {
    launch_url(validated_console_url(&url)?)
}

#[tauri::command]
pub fn open_external_status(url: String) -> Result<(), String> {
    launch_url(validated_external_status_url(&url)?)
}

#[cfg(test)]
mod tests {
    use super::{
        command_allowed, decode_and_consume_result, publish_command, validated_console_url,
        validated_external_status_url, PROTOCOL_SCHEMA,
    };
    use serde_json::{json, Map, Value};
    use std::fs;

    #[test]
    fn bridge_only_exposes_public_agent_commands() {
        assert!(command_allowed("session.reset"));
        assert!(command_allowed("metrics.query"));
        assert!(command_allowed("configuration.get"));
        assert!(command_allowed("configuration.update"));
        assert!(!command_allowed("shell.execute"));
        assert!(!command_allowed("configuration.write"));
    }

    #[test]
    fn bridge_preserves_the_agent_command_wire_contract() {
        let directory =
            std::env::temp_dir().join(format!("infra-sentinel-bridge-{}", uuid::Uuid::new_v4()));
        let mut payload = Map::new();
        payload.insert("bucket_seconds".to_owned(), json!(300));
        let receipt =
            publish_command(&directory, "metrics.query", payload).expect("publish command");
        let document = fs::read_to_string(directory.join(format!("{}.request.json", receipt.id)))
            .expect("read command");
        let value: Value = serde_json::from_str(&document).expect("decode command");
        assert_eq!(value["schema"], PROTOCOL_SCHEMA);
        assert_eq!(value["type"], "metrics.query");
        assert_eq!(value["payload"]["bucket_seconds"], 300);
        fs::remove_dir_all(directory).expect("remove test command directory");
    }

    #[test]
    fn command_results_are_removed_after_a_validated_read() {
        let directory =
            std::env::temp_dir().join(format!("infra-sentinel-result-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&directory).expect("create result directory");
        let command_id = uuid::Uuid::new_v4().to_string();
        let path = directory.join(format!("{command_id}.result.json"));
        fs::write(
            &path,
            serde_json::to_vec(&json!({
                "schema": PROTOCOL_SCHEMA,
                "id": command_id,
                "type": "metrics.query",
                "status": "ok"
            }))
            .expect("encode result"),
        )
        .expect("write result");

        let result = decode_and_consume_result(&path, &command_id).expect("consume result");

        assert!(result.is_some());
        assert!(!path.exists());
        fs::remove_dir_all(directory).expect("remove result directory");
    }

    #[test]
    fn console_links_are_limited_to_literal_loopback_urls() {
        assert!(validated_console_url("http://127.0.0.1:4318/#health").is_ok());
        assert!(validated_console_url("http://[::1]:8790/").is_ok());
        assert!(validated_console_url("https://example.com/").is_err());
        assert!(validated_console_url("http://localhost:4318/").is_err());
        assert!(validated_console_url("file:///tmp/console.html").is_err());
    }

    #[test]
    fn external_status_links_are_https_and_provider_allowlisted() {
        assert!(validated_external_status_url("https://status.openai.com/").is_ok());
        assert!(
            validated_external_status_url("https://status.claude.com/incidents/example").is_ok()
        );
        assert!(validated_external_status_url("https://status.deepseek.com/").is_ok());
        assert!(validated_external_status_url("http://status.openai.com/").is_err());
        assert!(validated_external_status_url("https://example.com/").is_err());
        assert!(validated_external_status_url("https://status.openai.com@example.com/").is_err());
    }
}
