//! Persistent macOS menu-bar presence for Infra Sentinel.
//!
//! The Dock icon is intentionally colorful; the menu-bar icon is a separate
//! monochrome template image designed for a 16–18 px status area. It reflects
//! only the public Agent Projection and never performs collection itself.

use crate::projection_cache::ProjectionCache;
use serde_json::Value;
use std::thread;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};

const REFRESH_INTERVAL: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Indicator {
    Starting,
    Normal,
    Warning,
    Critical,
}

impl Indicator {
    fn icon(self) -> tauri::image::Image<'static> {
        match self {
            Self::Starting => tauri::include_image!("./icons/menu-starting.png"),
            Self::Normal => tauri::include_image!("./icons/menu-normal.png"),
            Self::Warning => tauri::include_image!("./icons/menu-warning.png"),
            Self::Critical => tauri::include_image!("./icons/menu-critical.png"),
        }
    }

    fn tooltip(self) -> &'static str {
        match self {
            Self::Starting => "Infra Sentinel — starting",
            Self::Normal => "Infra Sentinel — monitoring",
            Self::Warning => "Infra Sentinel — warning",
            Self::Critical => "Infra Sentinel — action needed",
        }
    }
}

fn status_from_projection(projection: &Value) -> Indicator {
    match projection
        .pointer("/overall/status")
        .and_then(Value::as_str)
        .unwrap_or("starting")
    {
        "healthy" | "ok" | "none" => Indicator::Normal,
        "warning" => Indicator::Warning,
        "critical" | "degraded" | "error" => Indicator::Critical,
        _ => Indicator::Starting,
    }
}

fn current_indicator(cache: &ProjectionCache) -> Indicator {
    cache
        .snapshot()
        .as_deref()
        .map(status_from_projection)
        .unwrap_or(Indicator::Starting)
}

fn show_dashboard(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn watch_indicator(tray: tauri::tray::TrayIcon, cache: ProjectionCache) {
    thread::Builder::new()
        .name("infra-menu-bar".to_owned())
        .spawn(move || {
            let mut previous = None;
            loop {
                let current = current_indicator(&cache);
                if previous != Some(current) {
                    let _ = tray.set_icon_with_as_template(Some(current.icon()), true);
                    let _ = tray.set_tooltip(Some(current.tooltip()));
                    previous = Some(current);
                }
                thread::sleep(REFRESH_INTERVAL);
            }
        })
        .expect("cannot start Infra Sentinel menu-bar watcher");
}

pub fn install(app: &AppHandle, cache: ProjectionCache) -> Result<(), String> {
    let open = MenuItem::with_id(
        app,
        "open-dashboard",
        "Open Infra Sentinel",
        true,
        None::<&str>,
    )
    .map_err(|error| error.to_string())?;
    let quit = MenuItem::with_id(app, "quit", "Quit Infra Sentinel", true, None::<&str>)
        .map_err(|error| error.to_string())?;
    let menu = Menu::with_items(app, &[&open, &quit]).map_err(|error| error.to_string())?;
    let initial = current_indicator(&cache);
    let tray = TrayIconBuilder::with_id("infra-sentinel")
        .menu(&menu)
        .icon(initial.icon())
        .icon_as_template(true)
        .tooltip(initial.tooltip())
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open-dashboard" => show_dashboard(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                show_dashboard(tray.app_handle());
            }
        })
        .build(app)
        .map_err(|error| error.to_string())?;
    watch_indicator(tray, cache);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{status_from_projection, Indicator};
    use serde_json::json;

    #[test]
    fn projection_status_maps_to_a_small_set_of_menu_bar_states() {
        assert_eq!(
            status_from_projection(&json!({"overall":{"status":"healthy"}})),
            Indicator::Normal
        );
        assert_eq!(
            status_from_projection(&json!({"overall":{"status":"warning"}})),
            Indicator::Warning
        );
        assert_eq!(
            status_from_projection(&json!({"overall":{"status":"critical"}})),
            Indicator::Critical
        );
    }
}
