// 东南大学校园网自动认证 —— Tauri 外壳
//
// 这个进程同时持有窗口和菜单栏图标，所以两者共享同一个生命周期：
//   · 关窗口 → 只是隐藏，程序继续在托盘里跑
//   · 托盘「退出」→ 整个程序结束（窗口也一起关）
// 认证逻辑仍然由 seu_net.py 负责，这里只做界面和进程管理。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, WebviewWindow,
};

/// 调用 Python 脚本时用的解释器与脚本路径，启动时确定。
struct Backend {
    python: String,
    script: PathBuf,
}

#[derive(Serialize)]
struct CommandOutput {
    ok: bool,
    stdout: String,
    stderr: String,
    code: i32,
}

/// 找到可用的 Python 3 和 seu_net.py。
///
/// 打包后脚本随 .app 一起分发，放在 Resources 目录；源码运行时它在项目根目录
/// （也就是 src-tauri 的上一级）。两边都要能找到，否则界面全是「检查失败」。
fn locate_backend(app: &tauri::AppHandle) -> Backend {
    let mut candidates: Vec<PathBuf> = Vec::new();

    // 1) 打包后的 Resources 目录
    if let Ok(dir) = app.path().resource_dir() {
        candidates.push(dir.join("seu_net.py"));
        candidates.push(dir.join("_up_").join("seu_net.py"));
    }
    // 2) 可执行文件旁边、以及往上一两级（开发时的 target/debug/ -> src-tauri/ -> 根目录）
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        for _ in 0..5 {
            let Some(current) = dir.clone() else { break };
            candidates.push(current.join("seu_net.py"));
            dir = current.parent().map(|p| p.to_path_buf());
        }
    }
    // 3) 当前工作目录
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("seu_net.py"));
    }

    let script = candidates
        .into_iter()
        .find(|p| p.exists())
        .unwrap_or_else(|| PathBuf::from("seu_net.py"));

    // 优先用打包时指定的解释器；找不到就退回系统自带的。
    let mut python = std::env::var("SEU_PYTHON").unwrap_or_default();
    if python.is_empty() {
        for candidate in ["/usr/bin/python3", "python3", "python"] {
            if Command::new(candidate)
                .arg("-c")
                .arg("import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
            {
                python = candidate.to_string();
                break;
            }
        }
    }
    if python.is_empty() {
        python = "python3".to_string();
    }

    Backend { python, script }
}

fn run_backend(backend: &Backend, args: &[&str], stdin: Option<&str>) -> CommandOutput {
    let mut cmd = Command::new(&backend.python);
    cmd.arg(&backend.script).args(args);
    // stdout/stderr 必须显式 piped。只设 stdin 的话 wait_with_output 拿不到
    // 任何内容，表现就是「退出码 0 但输出为空」。
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    cmd.stdin(if stdin.is_some() {
        Stdio::piped()
    } else {
        Stdio::null()
    });

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(err) => {
            return CommandOutput {
                ok: false,
                stdout: String::new(),
                stderr: format!("启动 Python 失败：{err}"),
                code: -1,
            }
        }
    };

    if let Some(text) = stdin {
        use std::io::Write;
        if let Some(mut handle) = child.stdin.take() {
            let _ = handle.write_all(text.as_bytes());
        }
    }

    match child.wait_with_output() {
        Ok(out) => CommandOutput {
            ok: out.status.success(),
            stdout: String::from_utf8_lossy(&out.stdout).to_string(),
            stderr: String::from_utf8_lossy(&out.stderr).to_string(),
            code: out.status.code().unwrap_or(-1),
        },
        Err(err) => CommandOutput {
            ok: false,
            stdout: String::new(),
            stderr: format!("等待 Python 结束失败：{err}"),
            code: -1,
        },
    }
}

// ---------------------------------------------------------------- 命令（前端调用）

#[tauri::command]
fn backend_path(state: tauri::State<'_, Backend>) -> String {
    format!("{} {}", state.python, state.script.display())
}

/// 跑 `seu_net.py status --json`，前端拿到结构化状态。
#[tauri::command]
fn get_status(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    let out = run_backend(&state, &["status", "--json"], None);
    if out.stdout.trim().is_empty() {
        // 拿不到 JSON 时把 stderr 和退出码一起报出来，不然前端只看到一句
        // 「EOF while parsing」，根本不知道是路径错了还是脚本崩了
        return Err(format!(
            "状态输出为空（退出码 {}）：{}",
            out.code,
            out.stderr.trim()
        ));
    }
    serde_json::from_str(&out.stdout).map_err(|e| {
        format!(
            "状态解析失败：{}｜原始输出前 120 字：{}",
            e,
            out.stdout.chars().take(120).collect::<String>()
        )
    })
}

/// 跑 `seu_net.py config --json`，拿脱敏后的配置。
#[tauri::command]
fn get_config(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    let out = run_backend(&state, &["config", "--json"], None);
    serde_json::from_str(&out.stdout).map_err(|e| format!("配置解析失败：{e}"))
}

/// 保存配置。密码留空表示沿用原来的。
#[tauri::command]
fn save_config(
    state: tauri::State<'_, Backend>,
    payload: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let text = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
    let out = run_backend(&state, &["save"], Some(&text));
    let parsed: serde_json::Value = serde_json::from_str(&out.stdout)
        .map_err(|_| format!("保存失败：{}", out.stderr.trim()))?;
    Ok(parsed)
}

/// 立即认证一次。
#[tauri::command]
fn login_once(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    let out = run_backend(&state, &["login", "--once"], None);
    Ok(serde_json::json!({
        "ok": out.code == 0,
        "code": out.code,
        "message": if out.stdout.trim().is_empty() { out.stderr } else { out.stdout },
    }))
}

#[tauri::command]
fn logout(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    let out = run_backend(&state, &["logout"], None);
    Ok(serde_json::json!({
        "ok": out.code == 0,
        "message": if out.stdout.trim().is_empty() { out.stderr } else { out.stdout },
    }))
}

/// 读日志尾部（含轮转文件）。
#[tauri::command]
fn read_log(state: tauri::State<'_, Backend>, lines: Option<usize>) -> String {
    let limit = lines.unwrap_or(120);
    let base = match state.script.parent() {
        Some(dir) => dir.to_path_buf(),
        None => PathBuf::from("."),
    };
    // 数据目录可能是 ~/Library/Application Support/...，先问一次 Python 拿准
    let probe = run_backend(&state, &["config", "--json"], None);
    let dir = serde_json::from_str::<serde_json::Value>(&probe.stdout)
        .ok()
        .and_then(|v| v.get("data_dir").and_then(|d| d.as_str()).map(PathBuf::from))
        .unwrap_or(base);

    let mut content = String::new();
    for name in ["login.log.1", "login.log"] {
        if let Ok(text) = std::fs::read_to_string(dir.join(name)) {
            content.push_str(&text);
        }
    }
    let tail: Vec<&str> = content.lines().rev().take(limit).collect();
    tail.into_iter().rev().collect::<Vec<_>>().join("\n")
}

/// 打开配置文件夹（Finder / 资源管理器）。
#[tauri::command]
fn open_data_dir(state: tauri::State<'_, Backend>) -> Result<(), String> {
    let probe = run_backend(&state, &["config", "--json"], None);
    let dir = serde_json::from_str::<serde_json::Value>(&probe.stdout)
        .ok()
        .and_then(|v| v.get("data_dir").and_then(|d| d.as_str()).map(String::from))
        .ok_or_else(|| "拿不到数据目录".to_string())?;
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg(&dir).spawn().map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "windows")]
    {
        Command::new("explorer").arg(&dir).spawn().map_err(|e| e.to_string())?;
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open").arg(&dir).spawn().map_err(|e| e.to_string())?;
    }
    Ok(())
}

// ---------------------------------------------------------------- 窗口 / 托盘

fn show_main(window: &WebviewWindow) {
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "打开主界面", true, None::<&str>)?;
    let login_item = MenuItem::with_id(app, "login", "立即认证", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&open_item, &sep, &login_item, &sep, &quit_item])?;

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .tooltip("东南大学校园网")
        .menu(&menu)
        .show_menu_on_left_click(false);

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }

    builder
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => {
                if let Some(window) = app.get_webview_window("main") {
                    show_main(&window);
                }
            }
            "login" => {
                // 托盘里直接触发认证：让前端去跑，免得两边各写一套逻辑
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.emit("tray-login", ());
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // 左键点图标 = 打开主界面（macOS 上右键才出菜单）
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(window) = tray.app_handle().get_webview_window("main") {
                    show_main(&window);
                }
            }
        })
        .build(app)?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            backend_path,
            get_status,
            get_config,
            save_config,
            login_once,
            logout,
            read_log,
            open_data_dir,
        ])
        .setup(|app| {
            let backend = locate_backend(app.handle());
            app.manage(backend);
            build_tray(app.handle())?;

            // 窗口配置里是 visible:false。等页面加载完再 show——直接 show 的话
            // WebView 还没画完就把空窗口亮出来，看到一片空白。
            // 没等到加载事件时兜底 1.2 秒后也显示，不至于一直看不见窗口。
            let fallback = app.get_webview_window("main");
            if let Some(window) = fallback.clone() {
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_millis(1200));
                    let _ = window.show();
                });
            }
            Ok(())
        })
        .on_page_load(|webview, _payload| {
            if webview.label() == "main" {
                let _ = webview.window().show();
                let _ = webview.window().set_focus();
            }
        })
        .on_window_event(|window, event| {
            // 点关闭按钮不退出程序，只是把窗口藏起来——托盘还在，随时能点回来。
            // 真正退出靠托盘的「退出」。
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}
