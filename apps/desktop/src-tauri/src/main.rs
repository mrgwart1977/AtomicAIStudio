//! Atomic Dream Engine — Tauri shell.
//!
//! The Python/FastAPI backend runs as a managed sidecar: it is spawned
//! when the app starts, probed for readiness, and killed when the app
//! exits. The UI learns the sidecar's ephemeral port and per-launch
//! session token through the `get_backend_info` command only — nothing
//! is baked into the frontend bundle.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use tauri::Manager;
use std::io::Read;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Clone, Serialize)]
struct BackendInfo {
    port: u16,
    token: String,
}

struct SidecarState {
    info: BackendInfo,
    child: Mutex<Option<Child>>,
}

fn pick_free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .expect("bind ephemeral port")
        .local_addr()
        .expect("local addr")
        .port()
}

/// Locate the backend sidecar. The frozen PyInstaller binary bundled
/// with the installer is authoritative; the dev venv / PATH Python are
/// only fallbacks for `cargo run` without bundled resources.
fn backend_command(port: u16, token: &str, data_dir: &Path) -> Option<Command> {
    // 1. Frozen backend binary (PyInstaller onedir, bundled as a resource).
    if let Ok(exe) = std::env::current_exe() {
        let bundled = exe
            .parent()
            .map(|p| p.join("backend").join("atomic-backend.exe"));
        if let Some(path) = bundled.filter(|p| p.exists()) {
            let mut cmd = Command::new(path);
            cmd.args([
                "--port",
                &port.to_string(),
                "--token",
                token,
                "--data-dir",
                &data_dir.to_string_lossy(),
            ]);
            return Some(cmd);
        }
    }

    // 2. Development: run the Python module. ATOMIC_BACKEND_DIR can
    //    override; otherwise resolve relative to the crate directory.
    let backend_dir = std::env::var("ATOMIC_BACKEND_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../backend"));
    if !backend_dir.join("app/main.py").exists() {
        return None;
    }
    // Interpreter resolution: explicit env override → project venv → PATH.
    // PATH alone is not enough — the system interpreter may lack FastAPI.
    let venv_py = backend_dir.join(".venv").join("Scripts").join("python.exe");
    let python = std::env::var("ATOMIC_PYTHON")
        .map(PathBuf::from)
        .ok()
        .filter(|p| p.exists())
        .or_else(|| venv_py.exists().then_some(venv_py))
        .unwrap_or_else(|| PathBuf::from("python"));
    let mut cmd = Command::new(python);
    cmd.args([
        "-m",
        "app.main",
        "--port",
        &port.to_string(),
        "--token",
        token,
        "--data-dir",
        &data_dir.to_string_lossy(),
    ])
    .current_dir(&backend_dir)
    .env("PYTHONPATH", &backend_dir)
    .env("PYTHONUNBUFFERED", "1")
    .stdout(Stdio::null())
    .stderr(Stdio::null());
    Some(cmd)
}

fn wait_for_backend(child: &mut Child, port: u16, timeout: Duration) -> Result<(), String> {
    let start = Instant::now();
    while start.elapsed() < timeout {
        // A backend that died during startup (e.g. data-dir lock held by
        // another instance) should fail fast instead of waiting out the
        // full timeout.
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "backend sidecar exited during startup ({status}) — see the \
                 data-dir logs; another instance may already be running"
            ));
        }
        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) {
            // Probe /health — TCP accept alone does not mean uvicorn is serving.
            let req = b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n";
            use std::io::Write;
            if stream.write_all(req).is_ok() {
                let mut buf = [0u8; 512];
                if let Ok(n) = stream.read(&mut buf) {
                    if n > 0 && String::from_utf8_lossy(&buf[..n]).contains("200") {
                        return Ok(());
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    Err("backend sidecar did not become ready in time".into())
}

fn data_dir() -> PathBuf {
    // The "AtomicAIStudio" directory name predates the Atomic Dream Engine
    // rebrand and is intentionally retained: renaming it would orphan the
    // existing database, models, runtimes, and content-addressed assets.
    // The Tauri bundle identifier does NOT drive this path.
    std::env::var("ATOMIC_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            std::env::var("LOCALAPPDATA")
                .map(|p| PathBuf::from(p).join("AtomicAIStudio"))
                .unwrap_or_else(|_| PathBuf::from("data"))
        })
}

#[tauri::command]
fn get_backend_info(state: tauri::State<'_, SidecarState>) -> BackendInfo {
    state.info.clone()
}

fn main() {
    // ATOMIC_SIDECAR_TOKEN lets integration tests drive the API directly;
    // production launches generate a fresh per-launch UUID.
    let token = std::env::var("ATOMIC_SIDECAR_TOKEN")
        .ok()
        .filter(|t| t.len() >= 8)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let port = pick_free_port();
    let dir = data_dir();
    std::fs::create_dir_all(&dir).ok();

    let mut cmd = backend_command(port, &token, &dir).unwrap_or_else(|| {
        eprintln!("[atomic] backend not found (no packaged binary, no dev source)");
        std::process::exit(2);
    });
    let mut child = cmd.spawn().expect("spawn backend sidecar");

    if let Err(e) = wait_for_backend(&mut child, port, Duration::from_secs(30)) {
        eprintln!("[atomic] {e}");
        // Clean up the half-started child rather than orphaning it.
        let mut c = child;
        let _ = c.kill();
        std::process::exit(3);
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(SidecarState {
            info: BackendInfo {
                port,
                token: token.clone(),
            },
            child: Mutex::new(Some(child)),
        })
        .invoke_handler(tauri::generate_handler![get_backend_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                let state = app_handle.state::<SidecarState>();
                let child_opt = { state.child.lock().unwrap().take() };
                if let Some(mut child) = child_opt {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}
