use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

struct BackendProcess(Mutex<Option<Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let exe_dir = std::env::current_exe()
                .ok().and_then(|p| p.parent().map(|d| d.to_path_buf()))
                .expect("Cannot determine executable directory");

            let backend_path = resource_path(&exe_dir, "dist/chat-analyzer-backend");
            let wx_path = resource_path(&exe_dir, "dist/wx");
            let config_path = resource_path(&exe_dir, "config.json");

            if let Some(backend_dir) = backend_path.parent() {
                let wx_dest = backend_dir.join("wx");
                if wx_path.exists() && !wx_dest.exists() {
                    let _ = std::fs::copy(&wx_path, &wx_dest);
                }
            }

            // Clear macOS quarantine attributes on bundled binaries
            let _ = std::process::Command::new("xattr")
                .args(["-cr", &backend_path.to_string_lossy().to_string()])
                .output();
            let _ = std::process::Command::new("xattr")
                .args(["-cr", &wx_path.to_string_lossy().to_string()])
                .output();

            let config_arg = format!("--config={}", config_path.display());
            let resources_dir = exe_dir.parent().unwrap().join("Resources");

            match Command::new(&backend_path)
                .arg(&config_arg)
                .current_dir(&resources_dir)
                .spawn()
            {
                Ok(c) => {
                    app.manage(BackendProcess(Mutex::new(Some(c))));
                }
                Err(e) => {
                    if let Some(window) = app.get_webview_window("main") {
                        let msg = e.to_string().replace('\\', "\\\\").replace('\'', "\\'");
                        let _ = window.eval(&format!(
                            "document.body.innerHTML = '<div style=\"color:#f7626a;padding:40px;font-family:sans-serif;line-height:1.8\"><h2>启动失败</h2><p>无法启动后端服务</p><p><code>{}</code></p></div>';",
                            msg
                        ));
                    }
                }
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                // Kill the backend process chain
                kill_backend(&window);
                // Kill any lingering Flask processes on port 8899
                let _ = std::process::Command::new("pkill")
                    .args(["-f", "chat-analyzer-backend"])
                    .spawn();
                // Actually quit the app (macOS default keeps it alive after closing window)
                std::process::exit(0);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn resource_path(exe_dir: &std::path::Path, relative: &str) -> std::path::PathBuf {
    exe_dir.parent().unwrap().join("Resources").join("_up_").join(relative)
}

fn kill_backend(window: &tauri::Window) {
    if let Some(state) = window.try_state::<BackendProcess>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(ref mut child) = *guard {
                // Kill the child process
                let _ = child.kill();
                // Wait with timeout to avoid hanging
                for _ in 0..50 {
                    match child.try_wait() {
                        Ok(Some(_)) => break,  // exited
                        Ok(None) => std::thread::sleep(std::time::Duration::from_millis(20)),
                        Err(_) => break,
                    }
                }
                *guard = None;
            }
        }
    }
    // Force-kill any remaining process on port 8899
    let _ = std::process::Command::new("lsof")
        .args(["-ti", ":8899"])
        .output()
        .ok()
        .and_then(|o| {
            if o.status.success() {
                let pids = String::from_utf8_lossy(&o.stdout);
                for pid in pids.lines() {
                    let _ = std::process::Command::new("kill")
                        .args(["-9", pid.trim()])
                        .output();
                }
            }
            Some(())
        });
}
