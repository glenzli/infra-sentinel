#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod agent_bridge;
mod agent_supervisor;
mod app_paths;
mod menu_bar;
mod native_notifications;
mod projection_cache;

use projection_cache::ProjectionCache;

fn main() {
    let projection_cache = ProjectionCache::default();
    let builder = tauri::Builder::default();
    let builder = match agent_supervisor::static_demo_locale() {
        Some(locale) => builder.append_invoke_initialization_script(format!(
            "window.__INFRA_SENTINEL_STATIC_DEMO_LOCALE = {locale:?};"
        )),
        None => builder,
    };
    builder
        .manage(projection_cache.clone())
        .setup(move |app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            menu_bar::install(app.handle(), projection_cache.clone())
                .map_err(std::io::Error::other)?;
            agent_supervisor::start(app.handle().clone(), projection_cache.clone())
                .map_err(std::io::Error::other)?;
            native_notifications::start(app.handle().clone(), projection_cache.clone())
                .map_err(std::io::Error::other)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            agent_bridge::read_projection,
            agent_bridge::read_agent_command_result,
            agent_bridge::submit_agent_command,
            agent_bridge::open_console,
            agent_bridge::open_external_status,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Infra Sentinel desktop shell");
}
