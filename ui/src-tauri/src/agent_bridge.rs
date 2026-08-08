//! Narrow desktop-to-Agent bridge.
//!
//! The frontend cannot read arbitrary files or start arbitrary processes. It
//! can only consume the public Agent Projection and submit an allowlisted
//! command document. The Python Agent remains the sole owner of collection,
//! metric storage, policy evaluation, and projection construction.

use serde::Serialize;
use serde_json::{json, Map, Value};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

const PROTOCOL_SCHEMA: &str = "20260808.4";
const STATE_DIRECTORY_ENV: &str = "INFRA_SENTINEL_STATE_DIR";

#[derive(Serialize)]
pub struct CommandReceipt {
    id: String,
}

fn state_dir() -> Result<PathBuf, String> {
    if let Some(configured) = env::var_os(STATE_DIRECTORY_ENV) {
        return Ok(PathBuf::from(configured));
    }
    dirs::data_dir()
        .map(|directory| directory.join("Infra Sentinel").join("state"))
        .ok_or_else(|| "cannot determine an application data directory".to_owned())
}

fn command_allowed(command_type: &str) -> bool {
    matches!(command_type, "session.reset" | "metrics.query")
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
    fs::create_dir_all(commands).map_err(|error| format!("cannot create command directory: {error}"))?;
    let document = json!({
        "schema": PROTOCOL_SCHEMA,
        "id": command_id,
        "type": command_type,
        "payload": payload,
        "requested_at": requested_at(),
    });
    let temporary = commands.join(format!(".{command_id}.tmp"));
    let request = commands.join(format!("{command_id}.request.json"));
    let encoded = serde_json::to_vec(&document).map_err(|error| format!("cannot encode command: {error}"))?;
    fs::write(&temporary, encoded).map_err(|error| format!("cannot write command: {error}"))?;
    fs::rename(&temporary, &request).map_err(|error| format!("cannot publish command: {error}"))?;
    Ok(CommandReceipt { id: command_id })
}

fn write_command(command_type: &str, payload: Map<String, Value>) -> Result<CommandReceipt, String> {
    publish_command(&state_dir()?.join("commands"), command_type, payload)
}

fn decode_projection(document: &str) -> Result<Value, String> {
    let projection: Value = serde_json::from_str(document)
        .map_err(|error| format!("Agent Projection is not valid JSON: {error}"))?;
    if projection.get("schema").and_then(Value::as_str) != Some(PROTOCOL_SCHEMA) {
        return Err("Agent Projection protocol version is unsupported".to_owned());
    }
    Ok(projection)
}

#[tauri::command]
pub fn read_projection() -> Result<Option<Value>, String> {
    let path = state_dir()?.join("projection.json");
    let document = match fs::read_to_string(path) {
        Ok(document) => document,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(format!("cannot read Agent Projection: {error}")),
    };
    decode_projection(&document).map(Some)
}

#[tauri::command]
pub fn submit_agent_command(command_type: String, payload: Value) -> Result<CommandReceipt, String> {
    let object = payload
        .as_object()
        .cloned()
        .ok_or_else(|| "Agent command payload must be an object".to_owned())?;
    write_command(&command_type, object)
}

#[cfg(test)]
mod tests {
    use super::{command_allowed, decode_projection, publish_command, PROTOCOL_SCHEMA};
    use serde_json::{json, Map, Value};
    use std::fs;

    #[test]
    fn bridge_only_exposes_public_agent_commands() {
        assert!(command_allowed("session.reset"));
        assert!(command_allowed("metrics.query"));
        assert!(!command_allowed("shell.execute"));
        assert!(!command_allowed("configuration.write"));
    }

    #[test]
    fn bridge_preserves_the_agent_command_wire_contract() {
        let directory = std::env::temp_dir().join(format!("infra-sentinel-bridge-{}", uuid::Uuid::new_v4()));
        let mut payload = Map::new();
        payload.insert("bucket_seconds".to_owned(), json!(300));
        let receipt = publish_command(&directory, "metrics.query", payload).expect("publish command");
        let document = fs::read_to_string(directory.join(format!("{}.request.json", receipt.id))).expect("read command");
        let value: Value = serde_json::from_str(&document).expect("decode command");
        assert_eq!(value["schema"], PROTOCOL_SCHEMA);
        assert_eq!(value["type"], "metrics.query");
        assert_eq!(value["payload"]["bucket_seconds"], 300);
        fs::remove_dir_all(directory).expect("remove test command directory");
    }

    #[test]
    fn bridge_rejects_an_incompatible_projection() {
        assert!(decode_projection(r#"{"schema":"19990101.1"}"#).is_err());
        assert!(decode_projection(&format!(r#"{{"schema":"{PROTOCOL_SCHEMA}","infra":{{}}}}"#)).is_ok());
    }
}
