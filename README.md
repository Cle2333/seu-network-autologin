# 东南大学校园网自动认证

开机自动完成校园网认证。菜单栏常驻，状态一眼可见，不用打开浏览器输账号密码。

宿舍连 `SEU-ISP`（运营商宽带）和教室连 `SEU-WLAN`（校园网）的**账号写法不一样**，
程序会自己试出能用哪一份，并记住这个网络该用哪份。

## 界面

一个固定 380×520 的小窗口，深色 / 浅色跟随系统：

```
┌──────────────────────────────┐
│                              │
│  ●  已联网                    │
│     外网可用，无需操作          │
│  ──────────────────────────  │
│  网络        未识别（不影响认证）│
│  本机         10.210.48.210   │
│  账号         213252649       │
│                              │
│  [      立即认证      ] [刷新] │
│                              │
│  ┌──────┬──────┐             │
│  │ 日志 │ 设置 │             │
│  └──────┴──────┘             │
│  ┌──────────────────────────┐│
│  │ 2026-11-21 21:06:57 ...  ││
│  └──────────────────────────┘│
└──────────────────────────────┘
```

菜单栏图标：`✓` 已联网 / `!` 未认证 / `?` 不在校园网。

**窗口和托盘是同一个进程**，所以语义很干净：

| 操作 | 结果 |
| --- | --- |
| 关窗口 | 最小化到菜单栏，程序继续跑 |
| 点菜单栏图标 | 窗口回来 |
| 托盘右键「退出」 | **真的全退**，不留任何进程 |

**不占 Dock**：以 Accessory 身份运行，只在菜单栏有一个图标。

**没有后台守护进程**：不会出现「图标没了但还在偷偷认证」的幽灵进程。
菜单栏图标在 = 在运行，图标消失 = 真的停了。

> 如果你还需要「关掉界面后断网也能自动重连」，那是另一套东西：
> `python3 seu_net.py install` 会装一个独立的系统定时任务。
> 默认不装，界面上也没做这个开关，因为多数人不需要两套同时在跑。

## 安装

### 方式一：下载打包好的 app

到 Releases 下载 `.dmg`，拖进「应用程序」。第一次打开提示「无法验证开发者」时右键 →「打开」。

> **需要系统里有 Python 3**：认证逻辑跑在 `seu_net.py` 上。
> - macOS：装 Command Line Tools 即可（`xcode-select --install`）
> - Linux：一般自带
> - Windows：到 [python.org](https://www.python.org/downloads/) 装，勾选 "Add python.exe to PATH" 

### 方式二：自己构建

```bash
git clone <repo> && cd seu-network-autologin
./build.sh
```

需要 [Rust](https://rustup.rs)。Tauri CLI 会自动装。

## 配置

第一次打开会直接进「设置」，填账号密码即可。之后随时可以点「设置」改。

| 项 | 说明 |
| --- | --- |
| 宿舍宽带账号 | 一卡通号 |
| 宿舍宽带密码 | 留空 = 不修改 |
| 运营商 | 不确定就选默认，三家都试 |
| 教室用另一个号 | 勾上会展开教室的账号密码 |

**宿舍和教室不是同一个号时**，勾上最后一项单独填。密码留空则和宿舍共用。

配置文件位置：

```
~/Library/Application Support/SEU-Net-AutoLogin/config.json     # macOS
%APPDATA%\SEU-Net-AutoLogin\config.json                          # Windows
```

每次保存都会留 3 份历史（`.bak.1` ~ `.bak.3`），改错了能翻回去。

## 命令行

图形界面之外，`seu_net.py` 也能单独用（纯标准库，无依赖）：

```bash
python3 seu_net.py status        # 看状态
python3 seu_net.py status --json # 机器可读
python3 seu_net.py               # 前台认证
python3 seu_net.py login --once  # 只试一轮
python3 seu_net.py logout        # 下线（本校门户实测会拒绝）
python3 seu_net.py doctor        # 环境自检
python3 seu_net.py install       # 开机自启（launchd / 计划任务）
python3 seu_net.py uninstall     # 卸载自启
```

## 工作原理

| 动作 | 请求 |
| --- | --- |
| 探测门户 | `GET http://w.seu.edu.cn/`（回 Dr.COM 页面即认定） |
| 登录 | `GET :801/eportal/?c=ACSetting&a=Login&DDDDD=<账号>&upass=<密码>` |
| 查在线 | `GET http://w.seu.edu.cn/drcom/chkstatus?callback=dr1002` |

几个实测的坑：

- **端口不统一**：登录在 801(http)/802(https)，状态查询在 **80**。801 上查状态是 404。
- **老接口已失效**：`?c=Portal&a=login` 现在返回 500，必须用 `?c=ACSetting&a=Login`。
- **注销本校门户不支持**：返回 `Logout failed.`，即使确实在线。所以 `logout` 会如实报失败。
- **「终端IP已在线」不是密码错**：那是本机已有会话，程序会继续校验外网通不通。
- **macOS 读不到 SSID**：未授权定位时只给 `<redacted>`。不影响认证，用网关 + 网段识别环境。

**认证成功与否以外网是否真的通为准** —— 门户返回什么都不算数。

## 项目结构

```
seu_net.py                认证逻辑（纯标准库，可独立使用）
src-tauri/src/main.rs     Tauri 外壳：窗口 + 托盘 + 调 Python
src-tauri/tauri.conf.json Tauri 配置
ui/                       前端（HTML + CSS + 原生 JS，无框架）
build.sh                  打包脚本
AGENTS.md                 开发约定与踩坑记录
```

旧的 tkinter + pyobjc 实现在 `legacy` 分支。

## 说明与免责

- 配置文件是明文密码，已在 `.gitignore` 里，别提交。
- 只在东南大学网络环境下验证过。
- 仅供个人便利使用，请遵守学校的网络管理规定。
- MIT 协议，见 [LICENSE](LICENSE)。
