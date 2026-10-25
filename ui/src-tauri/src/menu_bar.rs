//! Persistent desktop tray / macOS menu-bar presence for Infra Sentinel.
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
        #[cfg(target_os = "windows")]
        return self.windows_icon();
        #[cfg(not(target_os = "windows"))]
        match self {
            Self::Starting => tauri::include_image!("./icons/menu-starting.png"),
            Self::Normal => tauri::include_image!("./icons/menu-normal.png"),
            Self::Warning => tauri::include_image!("./icons/menu-warning.png"),
            Self::Critical => tauri::include_image!("./icons/menu-critical.png"),
        }
    }

    #[cfg(target_os = "windows")]
    fn windows_icon(self) -> tauri::image::Image<'static> {
        // macOS tints template icons automatically. Windows needs its own
        // contrasting outline and color on both light and dark taskbars.
        let color = match self {
            Self::Starting => [66, 124, 213, 255],
            Self::Normal => [34, 156, 96, 255],
            Self::Warning => [218, 137, 19, 255],
            Self::Critical => [216, 54, 64, 255],
        };
        let mut pixels = vec![0_u8; 32 * 32 * 4];
        for y in 0..32 {
            for x in 0..32 {
                let radius = (x as f32 - 15.5).powi(2) + (y as f32 - 15.5).powi(2);
                let pixel = if radius > 14.5_f32.powi(2) {
                    [0, 0, 0, 0]
                } else if radius > 13.5_f32.powi(2) {
                    [28, 35, 44, 255]
                } else if radius > 12.5_f32.powi(2)
                    || ((14..18).contains(&x) && ((8..12).contains(&y) || (15..24).contains(&y)))
                {
                    [245, 248, 252, 255]
                } else {
                    color
                };
                let offset = (y * 32 + x) * 4;
                pixels[offset..offset + 4].copy_from_slice(&pixel);
            }
        }
        tauri::image::Image::new_owned(pixels, 32, 32)
    }

    fn tooltip(self) -> &'static str {
        match self {
            Self::Starting => "Infra Sentinel — starting",
            Self::Normal => "Infra Sentinel — monitoring",
            Self::Warning => "Infra Sentinel — attention needed",
            Self::Critical => "Infra Sentinel — action needed",
        }
    }
}

fn status_from_projection(projection: &Value) -> Indicator {
    match projection
        .pointer("/infra/overall/status")
        .and_then(Value::as_str)
        .unwrap_or("starting")
    {
        "healthy" | "ok" | "none" => Indicator::Normal,
        "warning" | "degraded" => Indicator::Warning,
        "critical" | "error" => Indicator::Critical,
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

pub fn show_dashboard(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
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
                    let _ = tray
                        .set_icon_with_as_template(Some(current.icon()), cfg!(target_os = "macos"));
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
        .icon_as_template(cfg!(target_os = "macos"))
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
            status_from_projection(&json!({"infra":{"overall":{"status":"healthy"}}})),
            Indicator::Normal
        );
        assert_eq!(
            status_from_projection(&json!({"infra":{"overall":{"status":"warning"}}})),
            Indicator::Warning
        );
        assert_eq!(
            status_from_projection(&json!({"infra":{"overall":{"status":"degraded"}}})),
            Indicator::Warning
        );
        assert_eq!(
            status_from_projection(&json!({"infra":{"overall":{"status":"critical"}}})),
            Indicator::Critical
        );
        assert_eq!(
            status_from_projection(&json!({"infra":{"overall":{"status":"error"}}})),
            Indicator::Critical
        );
        assert_eq!(
            status_from_projection(&json!({"infra":{"overall":{"status":"unknown"}}})),
            Indicator::Starting
        );
        assert_eq!(
            status_from_projection(&json!({"overall":{"status":"critical"}})),
            Indicator::Starting
        );
    }
}
