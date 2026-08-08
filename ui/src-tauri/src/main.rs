#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod agent_bridge;

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            agent_bridge::read_projection,
            agent_bridge::submit_agent_command,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Infra Sentinel desktop shell");
}
