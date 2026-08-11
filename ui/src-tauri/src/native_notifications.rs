//! App-owned macOS notifications for Agent alert events.
//!
//! The Agent records privacy-bounded events in its Projection. This module
//! observes that public state and presents notifications from the Infra
//! Sentinel process itself, so a notification click belongs to this app rather
//! than to an `osascript` helper.

use crate::app_paths::state_dir;
use serde_json::Value;
use std::fs;
use std::thread;
use std::time::Duration;
use tauri::AppHandle;

const POLL_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Clone, Debug, Eq, PartialEq)]
struct NotificationEvent {
    id: String,
    title: String,
    body: String,
}

fn bytes(value: Option<u64>) -> String {
    let mut amount = value.unwrap_or(0) as f64;
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"] {
        if amount < 1024.0 || unit == "TiB" {
            return if unit == "B" {
                format!("{} {unit}", amount as u64)
            } else {
                format!("{amount:.1} {unit}")
            };
        }
        amount /= 1024.0;
    }
    unreachable!()
}

fn number(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64)
}

fn notification_event(projection: &Value) -> Option<NotificationEvent> {
    let event = projection.get("last_event")?.as_object()?;
    let id = event.get("id")?.as_str()?.to_owned();
    let label = event
        .get("alert_group")
        .and_then(Value::as_str)
        .unwrap_or("Infra Sentinel");
    let event_type = event.get("type").and_then(Value::as_str).unwrap_or("alert");
    let level = event.get("level").and_then(Value::as_str).unwrap_or("warning");
    let scope = event.get("scope").and_then(Value::as_str);
    let title = match (scope, event_type, level) {
        (Some("system_resources"), "recovered", _) => "本机系统已恢复".to_owned(),
        (Some("system_resources"), "deescalated", _) => "本机系统状态改善".to_owned(),
        (Some("system_resources"), _, "critical") => "本机系统严重压力".to_owned(),
        (Some("system_resources"), _, _) => "本机系统需关注".to_owned(),
        (Some("upstream_status"), "recovered", _) => format!("{label} 已恢复"),
        (Some("upstream_status"), "deescalated", _) => format!("{label} 状态改善"),
        (Some("upstream_status"), _, "critical") => format!("{label} 严重服务异常"),
        (Some("upstream_status"), _, _) => format!("{label} 服务异常"),
        (_, "recovered", _) => format!("{label} 已恢复"),
        (_, "deescalated", _) => format!("{label} 告警降级"),
        (_, _, "critical") => format!("{label} 严重告警"),
        _ => format!("{label} 流量告警"),
    };
    let body = if scope == Some("upstream_status") || scope == Some("system_resources") {
        event
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("请查看官方状态页")
            .to_owned()
    } else if scope == Some("vps_daily_usage") {
        format!(
            "今日 {} · 阈值 {}",
            bytes(number(event.get("usage_bytes"))),
            bytes(number(event.get("threshold_bytes")))
        )
    } else if event_type == "recovered" {
        "流量已回落至阈值以下".to_owned()
    } else {
        let windows = event.get("windows").and_then(Value::as_object);
        let key = if level == "critical" { "critical" } else { "warning" };
        let totals = windows
            .and_then(|windows| windows.get(key))
            .and_then(Value::as_object)
            .map(|window| {
                number(window.get("up_bytes"))
                    .unwrap_or(0)
                    .saturating_add(number(window.get("down_bytes")).unwrap_or(0))
            });
        format!("当前累计 {}", bytes(totals))
    };
    Some(NotificationEvent { id, title, body })
}

fn read_event() -> Option<NotificationEvent> {
    let path = state_dir().ok()?.join("projection.json");
    let document = fs::read_to_string(path).ok()?;
    let projection: Value = serde_json::from_str(&document).ok()?;
    notification_event(&projection)
}

#[cfg(target_os = "macos")]
#[allow(deprecated)]
fn show_native(event: &NotificationEvent) {
    use objc2_foundation::{NSString, NSUserNotification, NSUserNotificationCenter};

    let notification = NSUserNotification::new();
    notification.setTitle(Some(&NSString::from_str(&event.title)));
    notification.setInformativeText(Some(&NSString::from_str(&event.body)));
    NSUserNotificationCenter::defaultUserNotificationCenter().deliverNotification(&notification);
}

#[cfg(not(target_os = "macos"))]
fn show_native(_: &NotificationEvent) {}

pub fn start(app: AppHandle) -> Result<(), String> {
    thread::Builder::new()
        .name("infra-native-notifications".to_owned())
        .spawn(move || {
            let mut latest_id = read_event().map(|event| event.id);
            loop {
                if let Some(event) = read_event() {
                    if latest_id.as_deref() != Some(event.id.as_str()) {
                        latest_id = Some(event.id.clone());
                        let _ = app.run_on_main_thread(move || show_native(&event));
                    }
                }
                thread::sleep(POLL_INTERVAL);
            }
        })
        .map_err(|error| format!("cannot start native notification watcher: {error}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::notification_event;
    use serde_json::json;

    #[test]
    fn daily_usage_alerts_keep_the_host_label_and_byte_values() {
        let event = notification_event(&json!({
            "last_event": {"id": "event-1", "type": "alert", "level": "critical", "scope": "vps_daily_usage", "alert_group": "Primary VPS", "usage_bytes": 1073741824_u64, "threshold_bytes": 536870912_u64}
        }))
        .expect("notification event");
        assert_eq!(event.title, "Primary VPS 严重告警");
        assert_eq!(event.body, "今日 1.0 GiB · 阈值 512.0 MiB");
    }

    #[test]
    fn upstream_alerts_use_service_language_and_incident_summary() {
        let event = notification_event(&json!({
            "last_event": {"id": "event-2", "type": "alert", "level": "warning", "scope": "upstream_status", "alert_group": "Claude", "description": "Elevated API errors"}
        }))
        .expect("notification event");
        assert_eq!(event.title, "Claude 服务异常");
        assert_eq!(event.body, "Elevated API errors");
    }

    #[test]
    fn system_alerts_use_host_pressure_language() {
        let event = notification_event(&json!({
            "last_event": {"id": "event-3", "type": "alert", "level": "critical", "scope": "system_resources", "alert_group": "本机系统", "description": "内存压力严重"}
        }))
        .expect("notification event");
        assert_eq!(event.title, "本机系统严重压力");
        assert_eq!(event.body, "内存压力严重");
    }
}
