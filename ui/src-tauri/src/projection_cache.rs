//! Validated in-memory owner for the latest Agent Projection.
//!
//! The sidecar supervisor feeds newline-delimited frames into this cache. UI,
//! tray, and notification consumers share immutable snapshots without polling
//! or rewriting the recovery checkpoint.

use serde_json::Value;
use std::fs;
use std::path::Path;
use std::sync::{Arc, RwLock};

pub const PROTOCOL_SCHEMA: &str = "20260812.1";

#[derive(Clone, Default)]
pub struct ProjectionCache {
    latest: Arc<RwLock<Option<Arc<Value>>>>,
}

pub fn decode_projection(document: &str) -> Result<Value, String> {
    let projection: Value = serde_json::from_str(document)
        .map_err(|error| format!("Agent Projection is not valid JSON: {error}"))?;
    if projection.get("schema").and_then(Value::as_str) != Some(PROTOCOL_SCHEMA) {
        return Err("Agent Projection protocol version is unsupported".to_owned());
    }
    Ok(projection)
}

impl ProjectionCache {
    pub fn replace_document(&self, document: &str) -> Result<(), String> {
        let projection = Arc::new(decode_projection(document)?);
        let mut latest = self
            .latest
            .write()
            .map_err(|_| "Agent Projection cache is unavailable".to_owned())?;
        *latest = Some(projection);
        Ok(())
    }

    pub fn snapshot(&self) -> Option<Arc<Value>> {
        self.latest.read().ok()?.clone()
    }

    pub fn load_checkpoint(&self, path: &Path) -> Result<bool, String> {
        let document = match fs::read_to_string(path) {
            Ok(document) => document,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(format!("cannot read Agent Projection checkpoint: {error}")),
        };
        self.replace_document(&document)?;
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::{decode_projection, ProjectionCache, PROTOCOL_SCHEMA};
    use std::fs;

    #[test]
    fn cache_rejects_incompatible_frames_and_keeps_the_last_valid_projection() {
        let cache = ProjectionCache::default();
        cache
            .replace_document(&format!(
                r#"{{"schema":"{PROTOCOL_SCHEMA}","updated_at":"first"}}"#
            ))
            .expect("cache valid projection");

        assert!(cache
            .replace_document(r#"{"schema":"19990101.1"}"#)
            .is_err());
        assert_eq!(
            cache
                .snapshot()
                .and_then(|value| value["updated_at"].as_str().map(str::to_owned)),
            Some("first".to_owned())
        );
    }

    #[test]
    fn recovery_checkpoint_is_loaded_once_into_memory() {
        let directory =
            std::env::temp_dir().join(format!("infra-projection-cache-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&directory).expect("create checkpoint directory");
        let checkpoint = directory.join("projection.json");
        fs::write(
            &checkpoint,
            format!(r#"{{"schema":"{PROTOCOL_SCHEMA}","updated_at":"checkpoint"}}"#),
        )
        .expect("write checkpoint");
        let cache = ProjectionCache::default();

        assert!(cache.load_checkpoint(&checkpoint).expect("load checkpoint"));
        fs::remove_file(&checkpoint).expect("remove checkpoint after load");
        assert_eq!(
            cache
                .snapshot()
                .and_then(|value| value["updated_at"].as_str().map(str::to_owned)),
            Some("checkpoint".to_owned())
        );
        fs::remove_dir_all(directory).expect("remove checkpoint directory");
    }

    #[test]
    fn decoder_requires_the_current_protocol_schema() {
        assert!(decode_projection(r#"{"schema":"19990101.1"}"#).is_err());
        assert!(decode_projection(&format!(r#"{{"schema":"{PROTOCOL_SCHEMA}"}}"#)).is_ok());
    }
}
