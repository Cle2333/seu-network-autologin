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
/// 手动实现 clone_for_task() 以便拷进 spawn_blocking，不占主线程。
struct Backend {
    python: String,
    script: PathBuf,
    /// 数据目录，第一次用到时算一次并缓存。用 Mutex 是因为要延迟初始化。
    data_dir: std::sync::Mutex<Option<PathBuf>>,
    /// 脚本路径本身是否可用；不可用时每个命令都直接报这个错
    problem: Option<String>,
}

impl Backend {
    /// 拿数据目录，首次调用时才去问 Python。
    fn data_dir(&self) -> PathBuf {
        let mut guard = match self.data_dir.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if let Some(dir) = guard.as_ref() {
            return dir.clone();
        }
        let dir = resolve_data_dir(self);
        *guard = Some(dir.clone());
        dir
    }

    /// 这个方法让 Backend 能安全地 clone 进 spawn_blocking。
    /// Mutex 不能 derive Clone，所以手写。
    fn clone_for_task(&self) -> Backend {
        let dir = self
            .data_dir
            .lock()
            .ok()
            .and_then(|g| g.clone());
        Backend {
            python: self.python.clone(),
            script: self.script.clone(),
            data_dir: std::sync::Mutex::new(dir),
            problem: self.problem.clone(),
        }
    }
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

    let found = candidates.into_iter().find(|p| p.exists());
    let problem = if found.is_none() {
        Some("找不到 seu_net.py：打包时应该放在 Resources 里，源码运行时在项目根目录".to_string())
    } else {
        None
    };
    let script = found.unwrap_or_else(|| PathBuf::from("seu_net.py"));

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
    let mut problem = problem;
    if python.is_empty() {
        problem = Some("找不到 python3，请先安装（macOS: xcode-select --install）".to_string());
        python = "python3".to_string();
    }

    Backend {
        python,
        script,
        data_dir: std::sync::Mutex::new(None),
        problem,
    }
}

/// 数据目录（config.json / login.log 所在处）。
///
/// 不在 setup() 里探测：那时 Tauri 运行时刚初始化，同步 spawn 子进程会把
/// 整个启动流程卡住（实测表现为界面空白、所有命令无响应）。
/// 改成第一次用到时算一次并缓存进 state。
fn resolve_data_dir(backend: &Backend) -> PathBuf {
    if backend.problem.is_some() {
        return backend
            .script
            .parent()
            .map(|p| p.to_path_buf())
            .unwrap_or_else(|| PathBuf::from("."));
    }
    let probe = run_backend(backend, &["config", "--json"], None);
    if let Ok(view) = serde_json::from_str::<serde_json::Value>(&probe.stdout) {
        if let Some(dir) = view.get("data_dir").and_then(|d| d.as_str()) {
            if !dir.is_empty() {
                return PathBuf::from(dir);
            }
        }
    }
    backend
        .script
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
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
//
// 全部是 async + spawn_blocking：Tauri 对同步命令是在主线程跑的，
// 而这里每个命令都要起一个 Python 进程、可能要等几秒（门户探测、认证），
// 同步实现会把整个窗口冻住。放进阻塞线程池就没这问题。

/// 把 Backend 拷一份丢进阻塞线程池里执行。
async fn blocking<T, F>(backend: &Backend, job: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce(Backend) -> Result<T, String> + Send + 'static,
{
    let backend = backend.clone_for_task();
    tauri::async_runtime::spawn_blocking(move || job(backend))
        .await
        .map_err(|e| format!("后台任务异常：{e}"))?
}

fn require_ready(backend: &Backend) -> Result<(), String> {
    match &backend.problem {
        Some(msg) => Err(msg.clone()),
        None => Ok(()),
    }
}

#[tauri::command]
async fn backend_path(state: tauri::State<'_, Backend>) -> Result<String, String> {
    Ok(format!("{} {}", state.python, state.script.display()))
}

/// 跑 `seu_net.py status --json`，前端拿到结构化状态。
#[tauri::command]
async fn get_status(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    blocking(&state, |backend| {
        require_ready(&backend)?;
        let out = run_backend(&backend, &["status", "--json"], None);
        if out.stdout.trim().is_empty() {
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
    })
    .await
}

/// 跑 `seu_net.py config --json`，拿脱敏后的配置。
#[tauri::command]
async fn get_config(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    blocking(&state, |backend| {
        require_ready(&backend)?;
        let out = run_backend(&backend, &["config", "--json"], None);
        serde_json::from_str(&out.stdout).map_err(|e| {
            format!("配置解析失败：{}｜{}", e, out.stderr.trim())
        })
    })
    .await
}

/// 保存配置。密码留空表示沿用原来的。
#[tauri::command]
async fn save_config(
    state: tauri::State<'_, Backend>,
    payload: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let text = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
    blocking(&state, move |backend| {
        require_ready(&backend)?;
        let out = run_backend(&backend, &["save"], Some(&text));
        serde_json::from_str(&out.stdout)
            .map_err(|_| format!("保存失败：{}", out.stderr.trim()))
    })
    .await
}

/// 立即认证一次。
///
/// 注意：seu_net.py 在「已有别的实例在认证」时会返回 0（本次跳过），
/// 那不是真的认证成功，所以不能只看退出码——要看输出里有没有「跳过」。
#[tauri::command]
async fn login_once(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    blocking(&state, |backend| {
        require_ready(&backend)?;
        let out = run_backend(&backend, &["login", "--once"], None);
        let message = if out.stdout.trim().is_empty() {
            out.stderr.clone()
        } else {
            out.stdout.clone()
        };
        let skipped = out.stdout.contains("已有另一个实例正在认证");
        Ok(serde_json::json!({
            "ok": out.code == 0 && !skipped,
            "skipped": skipped,
            "code": out.code,
            "message": message,
        }))
    })
    .await
}

#[tauri::command]
async fn logout(state: tauri::State<'_, Backend>) -> Result<serde_json::Value, String> {
    blocking(&state, |backend| {
        require_ready(&backend)?;
        let out = run_backend(&backend, &["logout"], None);
        Ok(serde_json::json!({
            "ok": out.code == 0,
            "message": if out.stdout.trim().is_empty() { out.stderr } else { out.stdout },
        }))
    })
    .await
}

/// 读日志尾部（含轮转文件）。数据目录在启动时已经缓存，这里不再多起进程。
#[tauri::command]
fn read_log(state: tauri::State<'_, Backend>, lines: Option<usize>) -> String {
    let limit = lines.unwrap_or(120);
    let dir = state.data_dir();
    // 日志名可配，但默认 login.log；把最新的几份按顺序拼起来
    let mut names: Vec<String> = Vec::new();
    for index in (1..=3).rev() {
        names.push(format!("login.log.{index}"));
    }
    names.push("login.log".to_string());

    let mut content = String::new();
    for name in names {
        if let Ok(text) = std::fs::read_to_string(dir.join(name)) {
            content.push_str(&text);
        }
    }
    if content.is_empty() {
        return String::new();
    }
    let tail: Vec<&str> = content.lines().rev().take(limit).collect();
    tail.into_iter().rev().collect::<Vec<_>>().join("\n")
}

/// 打开配置文件夹（Finder / 资源管理器）。
#[tauri::command]
fn open_data_dir(state: tauri::State<'_, Backend>) -> Result<(), String> {
    let dir = state.data_dir().to_string_lossy().to_string();
    if dir.is_empty() {
        return Err("拿不到数据目录".to_string());
    }
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

// ---------------------------------------------------------------- 开机自启
//
// 直接写系统自启项，不引第三方插件：
//   macOS   ~/Library/LaunchAgents/cn.edu.seu.net.autologin.gui.plist
//   Windows  HKCU\Software\Microsoft\Windows\CurrentVersion\Run
//
// 注意这和命令行版的 `seu_net.py install` 是两套东西：
// 那个装的是「定时跑 login --quiet」的后台任务，这个是「开机启动图形界面」。
// 两者可以同时存在，靠单实例锁互不影响。

const AUTOSTART_LABEL: &str = "cn.edu.seu.net.autologin.gui";

fn macos_plist_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home)
        .join("Library/LaunchAgents")
        .join(format!("{AUTOSTART_LABEL}.plist"))
}

#[cfg(target_os = "macos")]
fn autostart_enabled() -> bool {
    macos_plist_path().exists()
}

#[cfg(target_os = "windows")]
fn autostart_enabled() -> bool {
    // 用 run_hidden 避免无控制台程序弹黑窗
    run_hidden(Command::new("reg").args([
        "query",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        "/v",
        AUTOSTART_LABEL,
    ]))
    .map(|o| o.status.success())
    .unwrap_or(false)
}

#[cfg(all(unix, not(target_os = "macos")))]
fn autostart_enabled() -> bool {
    let home = std::env::var("HOME").unwrap_or_default();
    PathBuf::from(home)
        .join(".config/autostart")
        .join(format!("{AUTOSTART_LABEL}.desktop"))
        .exists()
}

#[tauri::command]
async fn autostart_state() -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(autostart_enabled)
        .await
        .map_err(|e| format!("后台任务异常：{e}"))
}

#[tauri::command]
async fn set_autostart(enabled: bool) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if enabled {
            enable_autostart()?;
        } else {
            disable_autostart()?;
        }
        Ok(autostart_enabled())
    })
    .await
    .map_err(|e| format!("后台任务异常：{e}"))?
}


/// XML 特殊字符转义。路径里可能有 & < >（比如家目录叫 R&D），
/// 不转义会写出畸形 plist，bootstrap 报个看不懂的错，而文件还在，
/// 于是下次打开界面勾选框显示为「已开启」但实际上从没生效过。
fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

#[cfg(target_os = "macos")]
fn enable_autostart() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let path = macos_plist_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let plist = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"#,
        label = AUTOSTART_LABEL,
        exe = xml_escape(&exe.display().to_string())
    );
    std::fs::write(&path, plist).map_err(|e| format!("写 plist 失败：{e}"))?;

    // 只写文件，不调 launchctl bootstrap。原因是 bootstrap 遇到 RunAtLoad=true
    // 会立刻把程序跑起来，而此刻已经有一个实例在跑了——勾一下勾选框就多出
    // 第二个托盘图标、第二个窗口。plist 放在 LaunchAgents 目录里，下次登录
    // 系统会自动加载，本次运行完全不受影响。
    Ok(())
}

#[cfg(target_os = "macos")]
fn disable_autostart() -> Result<(), String> {
    // 同样不调 launchctl bootout：如果当前这个进程就是由该 LaunchAgent 启动的，
    // bootout 会给它自己发 SIGTERM，取消勾选的那一刻程序自己被杀掉。
    // 删掉 plist 就够了——下次登录不再启动。
    let path = macos_plist_path();
    if path.exists() {
        std::fs::remove_file(&path).map_err(|e| format!("删除 plist 失败：{e}"))?;
    }
    Ok(())
}

/// 跑一个控制台程序，且不弹出窗口。
///
/// Windows 上本程序是 windows_subsystem="windows"（无控制台），此时启动 reg.exe
/// 这类控制台程序会新开一个黑窗口闪一下。加 CREATE_NO_WINDOW 压制掉。
fn run_hidden(cmd: &mut Command) -> std::io::Result<std::process::Output> {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.output()
}

#[cfg(target_os = "windows")]
fn enable_autostart() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    // 路径必须加引号，否则含空格的路径会被 Run 键按空格拆开，登录时静默失败
    let out = run_hidden(Command::new("reg").args([
        "add",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        "/v",
        AUTOSTART_LABEL,
        "/t",
        "REG_SZ",
        "/d",
        &format!("\"{}\"", exe.display()),
        "/f",
    ]))
    .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!("写注册表失败：{}", String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(())
}

#[cfg(target_os = "windows")]
fn disable_autostart() -> Result<(), String> {
    let _ = run_hidden(Command::new("reg").args([
        "delete",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        "/v",
        AUTOSTART_LABEL,
        "/f",
    ]));
    Ok(())
}

#[cfg(all(unix, not(target_os = "macos")))]
fn enable_autostart() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let home = std::env::var("HOME").unwrap_or_default();
    let dir = PathBuf::from(home).join(".config/autostart");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    // Exec 的值会按空格拆词，路径含空格（比如 ~/Downloads/My Apps/）就会拆坏。
    // 按 Desktop Entry 规范用双引号包起来，内部的 " ` $ \ 要转义。
    let quoted = exe
        .display()
        .to_string()
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('`', "\\`")
        .replace('$', "\\$");
    let content = format!(
        "[Desktop Entry]\nType=Application\nName=东南大学校园网\nExec=\"{quoted}\"\nX-GNOME-Autostart-enabled=true\n"
    );
    std::fs::write(dir.join(format!("{AUTOSTART_LABEL}.desktop")), content)
        .map_err(|e| e.to_string())
}

#[cfg(all(unix, not(target_os = "macos")))]
fn disable_autostart() -> Result<(), String> {
    let home = std::env::var("HOME").unwrap_or_default();
    let path = PathBuf::from(home)
        .join(".config/autostart")
        .join(format!("{AUTOSTART_LABEL}.desktop"));
    if path.exists() {
        std::fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
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
            autostart_state,
            set_autostart,
        ])
        .setup(|app| {
            let backend = locate_backend(app.handle());
            app.manage(backend);

            // 不占 Dock：这是个菜单栏小工具，Dock 里多一个图标纯属碍事。
            // Accessory 策略下窗口仍能正常显示和聚焦，只是不进 Dock、不占 Cmd+Tab。
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

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
            // 关窗口 = 最小化到托盘。进程继续跑，菜单栏图标还在——
            // 所以「图标在 = 在运行」这个语义是成立的。
            // 真的退出只能通过托盘右键菜单里的「退出」，那条路径会 exit(0)，
            // 不留任何后台进程。
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("Tauri 启动失败");
}
