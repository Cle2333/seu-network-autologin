# 东南大学校园网自动认证（macOS / Windows）

开机自动完成校园网认证，不用每次打开浏览器、输一卡通号和密码。

一个 Python 文件，只用标准库。**macOS 上系统自带 Python 就能跑，不用装任何东西。**

```bash
python3 seu_net.py          # 认证
python3 seu_net.py gui      # 打开小窗口
```

## 它解决什么

宿舍连 `SEU-ISP`（走运营商宽带）和教室连 `SEU-WLAN`（走校园网），**账号写法不一样**：
前者是 `一卡通号@cmcc`，后者是 `,0,一卡通号`。这台笔记本要带着到处跑，
所以程序会在连上网的那一刻判断「这一份写法能不能用」，能用就记住，下次直接命中。

判定方式不是猜 SSID，而是**真的提交一次并检查外网通不通**——门户说成功都不算数。

```bash
$ python3 seu_net.py status
网络：读不到 SSID（未授权定位，不影响认证）
本机：10.210.48.210    网关：10.210.0.1
门户：http://w.seu.edu.cn:801
门户状态：门户显示在线
账号：213253274@unicom
外网：通（HTTP 200，内容匹配）
```

## 快速开始

**双击启动**：macOS 双击 `启动.command`，Windows 双击 `启动.cmd`，直接打开图形界面。

第一次运行时窗口会**自动弹出设置框**，在界面里填一卡通号、密码、选一下运营商就能用——
不用去编辑任何文件，存完会自动认证一次。

命令行：

```bash
git clone <repo> && cd seu-network-autologin
python3 seu_net.py init      # 终端里问三个问题，生成 config.json
python3 seu_net.py           # 试一次认证，看到「认证完成」就成了
python3 seu_net.py install   # 装成开机自启（每 60 秒检查一次）
```

> 第一次双击 `启动.command` 如果提示「无法验证开发者」，右键 →「打开」即可；
> 或者 `chmod +x 启动.command` 一次。
> Windows 上双击后如果一闪而过，说明缺 Python——脚本会提示去 python.org 装（记得勾选
> “Add python.exe to PATH”）。

### 窗口一片空白？（macOS 常见）

macOS 系统自带的 Python 3（`/usr/bin/python3`）里带的是 **Tk 8.5**，它在 macOS 26 之后
画不出 Label / Entry / Frame 这类控件——只有 Button 和 Radiobutton 能显示，所以窗口看起来是空的。

启动脚本会自动跳过这类 Python，去找 Tk ≥ 8.6 的（自己装的 python.org 版本、Homebrew 的
`python-tk` 都满足）。一个都找不到时它会**明确告诉你**，而不是开个空白窗口让你猜：

```
没找到能用的图形界面环境。
原因：macOS 自带的 Python 3 使用 Tk 8.5，在新版 macOS 上画不出输入框和文字…
装一个带新版 Tk 的 Python 就行（任选一种）：
  · 命令行：brew install python-tk@3.12
  · 或者到 https://www.python.org/downloads/ 下载安装 Python 3.12+
不想装也可以先用命令行，功能一样：
  python3 seu_net.py init      # 配置账号
  python3 seu_net.py status    # 看状态
  python3 seu_net.py           # 认证
```

`python3 seu_net.py doctor` 也会直接报出当前的 Tk 版本和是否可用。

## 界面

```
校园网自动认证
macOS / Windows 通用 · 按网络环境自动挑选账号写法

┌──────────────────────────────────────────────┐
│  已联网                                       │
│  外网可用，不需要额外操作。                     │
│                                               │
│  网络   读不到（系统未授权定位，不影响认证）      │
│  本机   10.210.48.210    网关 10.210.0.1       │
│  门户   http://w.seu.edu.cn:801                │
│  外网   HTTP 200，内容匹配                      │
│  账号   213253274@unicom                       │
└──────────────────────────────────────────────┘

[立即认证] [注销下线] [刷新状态]
[设置账号] [配置原文] [打开日志]

最近日志
──────────────────────────────────────────────
```

「设置账号」随时可以改账号密码和运营商，改完直接保存。密码框是隐藏显示，
保存在 `config.json` 里（文件权限 0600，只有你自己能读）。

## 命令

| 命令 | 作用 |
| --- | --- |
| `seu_net.py` | 前台认证，一直重试到成功或超时 |
| `seu_net.py login --once` | 只试一轮（调试用） |
| `seu_net.py status` | 看当前状态，**只读**，不改网络 |
| `seu_net.py logout` | 让本机下线（本校门户实测会拒绝，见下文） |
| `seu_net.py gui` | 打开小窗口 |
| `seu_net.py init` | 首次配置向导 |
| `seu_net.py install [--interval 秒]` | 装成开机自启 |
| `seu_net.py uninstall` | 卸载自启 |
| `seu_net.py doctor` | 环境自检，出问题先跑这个 |

## 配置

跑 `init` 会生成 `config.json`，也可以照着 [config.example.json](config.example.json) 手写
（支持 `//` 注释）。核心就两块：

```jsonc
{
  "account": "一卡通号",
  "password": "密码",
  "profiles": [
    {"name": "宿舍走宽带", "ssids": ["SEU-ISP"],  "account_suffix": "@cmcc", "priority": 10},
    {"name": "教室走校园网", "ssids": ["SEU-WLAN"], "account_prefix": ",0,", "account_suffix": "", "priority": 20}
  ]
}
```

条目怎么挑（**这是「宿舍/教室自动切换」的关键**）：

1. 上次在这个网络里成功过的那条 —— 记在 `state.json`，命中就不用试别的
2. `ssids` 和当前 WiFi 名对得上的那条
3. 剩下的按 `priority` 从小到大依次试

每一条都会真的提交一次，外网通了才认。**写错的后缀会被自动跳过，不会卡住认证。**

> 所以就算你拿不准自己的账号该不该带 `@cmcc`，把两种都写上即可，程序自己会筛。

## macOS 上读不到 WiFi 名（正常现象）

从 macOS Sonoma 起，没有「定位服务」授权的程序读 WiFi 名会只拿到字面量 `<redacted>`。
**这不影响认证**：程序会改用「网关 + 本机网段」来识别网络环境，并按上面第 1 步记住这个网络该用哪份配置。
`doctor` 和 `status` 都会明确提示这一点，不是报错。

想让它显示真实 WiFi 名，可以在
`系统设置 → 隐私与安全性 → 定位服务` 里给终端（或 iTerm）授权——纯属好看，不授权也能正常用。

## 开机自启

`seu_net.py install` 在 macOS 上写 `~/Library/LaunchAgents/cn.edu.seu.net.autologin.plist`
（`RunAtLoad` + 每 60 秒），在 Windows 上建一个每 60 分钟的任务计划。

用的是 `/usr/bin/python3` 这种稳定路径，不写死带版本号的工具链目录，升级后不会静默失效。

```bash
python3 seu_net.py install --interval 90   # 改成每 90 秒
python3 seu_net.py uninstall               # 停用
```

Windows 上额外说明：任务计划在**用户登录后**才跑（不是锁屏阶段），并且
`schtasks` 计划任务的最小粒度是 1 分钟。

## 日志

`login.log`，超过 `log_max_kb`（默认 512 KB）就轮转成 `login.log.1`、`.2`、`.3`，
最多留 3 份备份，更旧的删掉。**总占用上限 = 4 × 512 KB = 2 MB，不会无限长大。**

同一条消息连续重复时只记一次，隔一分钟补一句「重复了几次」，避免断网时把日志刷爆。

## 认证是怎么做的

东南大学门户是 Drcom。认证由这几步组成：

| 动作 | 请求 |
| --- | --- |
| 探测门户 | `GET http://w.seu.edu.cn/`（回 Dr.COM 页面即认定） |
| 登录 | `GET :801/eportal/?c=ACSetting&a=Login&DDDDD=<账号>&upass=<密码>&wlanuserip=<本机IP>` |
| 注销 | `GET :801/eportal/?c=ACSetting&a=Logout`（见下方说明） |
| 查在线 | `GET http://w.seu.edu.cn/drcom/chkstatus?callback=dr1002` |

> **注意端口不统一**：登录/注销在 801(http)/802(https)，在线状态查询在 **80**。
> 在 801 上请求 `/drcom/chkstatus` 会 404，这是实测过的坑。

> **注销接口在本校门户实测返回 `Logout failed.`**（页面是 `Dr.COMWebLoginID_2.htm` 失败页），
> 即使本机明明在线。所以 `logout` 会如实报告「门户拒绝注销」而不是假装成功。
> 影响：认证流程里「门户说在线但外网不通」时会先试一次注销，失败就照常重新登录——
> 大多数情况重新登录本身就能救活会话。**`logout` 不能用来可靠地踢自己下线，
> 想断开就直接关 WiFi。**

登录返回成功时门户会给一段 `Msg=0` 或成功页；失败是 `Msg=01;msga='用户名或密码错误！'`。
但**这两种都不是最终判据**——程序会再请求一次外网，真通了才算认证成功
（未认证时请求会被门户劫持成登录页，内容对不上）。

## 用 `init` 不确定运营商时

第三问问「宿舍宽带是哪一家」时直接回车，程序会把**移动 `@cmcc`、电信 `@dx`、联通 `@lt` 三条都写上**，
认证时依次尝试，失败的那个自动跳过。所以不需要你事先搞清楚自己属于哪家。

确认之后可以手动删掉用不上的两条，认证会更快（少一次无效尝试）。

## 排查

先跑 `python3 seu_net.py doctor`。常见情况：

| 现象 | 原因与处理 |
| --- | --- |
| `未发现校园网门户` | 不在校园网，或 WiFi 还没连上。`wifi_auto_connect` 会主动去连 `wifi_order` 里的网络 |
| `用户名或密码错误` | 一卡通号/密码不对，或这一条的 `account_suffix` 不适用——程序会自动换下一条 |
| `门户显示在线` 但 `外网不通` | 会话卡住了，`login` 会先注销再重登 |
| 读不到 SSID | macOS 未授权定位，正常，不影响认证（见上文） |
| 装了自启但没生效 | `launchctl list \| grep seu` 看是否在跑；跑一次 `seu_net.py doctor` 确认账号已配 |

## 目录结构

```
seu_net.py            主程序（单文件，标准库，子命令切功能）
启动.command          macOS 双击打开界面
启动.cmd              Windows 双击打开界面
config.example.json   配置模板（带注释）
config.json           你的配置（含明文密码，已 gitignore）
state.json            记住哪个网络用哪份配置（自动生成，已 gitignore）
login.log             日志（自动轮转，已 gitignore）
```

## 说明与免责

- `config.json` 是明文密码，务必别提交、别外传。
- 只在东南大学网络环境下验证过；其他学校的 Drcom 门户接口可能不同，但地址、参数、
  后缀都能在 `config.json` 里改。
- 仅供个人便利使用，请遵守学校的网络管理规定。
- MIT 协议，见 [LICENSE](LICENSE)。

## 相关项目

| 项目 | 侧重 |
| --- | --- |
| [NN708/seu-wlan-login](https://github.com/NN708/seu-wlan-login) | 最流行的一键登录，面向一次性使用 |
| [ChenYuWuAi/seu-autologin](https://github.com/ChenYuWuAi/seu-autologin) | Linux + systemd |
| [geniezi/SEU-WLAN-LOGIN](https://github.com/geniezi/SEU-WLAN-LOGIN) | OpenWrt 路由器版 |
| [Flynn-woo/seu-campus-autologin-windows](https://github.com/Flynn-woo/seu-campus-autologin-windows) | Windows 工程化完整 |

本仓库定位：**macOS + Windows 同一套代码 + 按网络环境自动切换账号写法 +
真实外网校验 + 有图形界面 + 日志自动轮转**。
