#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod agent_bridge;
mod agent_supervisor;
mod app_paths;
mod menu_bar;
mod native_notifications;

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            menu_bar::install(app.handle()).map_err(std::io::Error::other)?;
            agent_supervisor::start(app.handle().clone()).map_err(std::io::Error::other)?;
            native_notifications::start(app.handle().clone()).map_err(std::io::Error::other)?;
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
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Infra Sentinel desktop shell");
}
