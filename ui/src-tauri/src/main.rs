#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod agent_bridge;
mod agent_supervisor;
mod app_paths;

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            agent_supervisor::start(app.handle().clone()).map_err(std::io::Error::other)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            agent_bridge::read_projection,
            agent_bridge::read_agent_command_result,
            agent_bridge::submit_agent_command,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Infra Sentinel desktop shell");
}
