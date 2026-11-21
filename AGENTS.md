# 东南大学校园网自动认证 —— 项目约定

单文件为主：`seu_net.py`（纯标准库），`seu_tray.py`（macOS 托盘，需 pyobjc）。

## 绝对不可违反

**不要删除、覆盖、清空运行时的用户数据。**

`config.json`（含明文账号密码）、`config.json.bak.*`、`state.json` 都是用户真实数据，
**不在任何「跑测试」的豁免范围内**。曾经用 `rm -f config.json` 制造「无配置」状态做测试，
把用户填好的账号一起删掉、且无法恢复。

- 测「没有配置」这类场景：用 `tempfile.mkdtemp()` 建临时目录，赋值 `S.BASE_DIR` /
  `S.CONFIG_PATH` / `S.STATE_PATH` / `S.LOCK_PATH` 指过去，测完 `shutil.rmtree` 临时目录
- 真要删用户数据：先说明删哪个、为什么，拿到确认再动手；删除用 `trash`，不用 `rm`
- 打包产物、日志、`.venv-*` 可以清，用户数据不行

## 构建与运行

```bash
python3 seu_net.py status      # 只读体检，改完先跑这个
python3 seu_net.py doctor      # 环境自检（含 Tk 版本、自启状态）
python3 seu_net.py gui         # 图形界面
./build_app.sh                 # 打包 .app（用 .venv-build，不污染系统）
```

必须同时验 Python 3.9（`/usr/bin/python3`）和 3.14（PATH 上的 `python3`）：
自启写的是 `/usr/bin/python3`，界面用的是带新 Tk 的解释器。

## 踩过的坑（改之前先看）

- **macOS 的 Tk 8.5 画不出界面**：`/usr/bin/python3` 带的 Tk 8.5 在 macOS 26+ 上
  Label / Entry / Frame 全部不渲染，窗口一片空白，只有 Button / Radiobutton 能显示。
  界面必须用 Tk ≥ 8.6。`doctor` 会报当前 Tk 版本。
- **门户端口不统一**：登录/注销在 `:801`(http) / `:802`(https) 的 `/eportal/`，
  在线状态查询在 **80** 端口的 `/drcom/chkstatus`。在 801 上查状态是 404。
- **登录接口**：`?c=ACSetting&a=Login`，字段 `DDDDD` / `upass`。
  老的 `?c=Portal&a=login` 现在返回 500。
- **注销在本校门户返回 `Logout failed.`**（即使确实在线）。`do_logout` 必须看返回内容，
  不能因为没抛异常就报成功。
- **macOS 读不到 SSID**：未授权定位时 `ipconfig getsummary` / CoreWLAN 只给字面量
  `<redacted>` 或 nil。别把它当网络名；用「网关 + 网段」识别环境。
- **`WatchPaths` 只在文件被 rename 替换时触发**，原地追加和 `touch` 都不触发。
  系统更新 `resolv.conf` 用 rename，所以有效。
- **`NSStatusItem` 的 `imagePosition`**：必须先 `setTitle_` 再 `setImage_` 最后
  `setImagePosition_`；顺序错了会被重置成 7（仅图标），菜单栏文字消失。
- **pyobjc 装在 `.venv-tray/`**，不是系统 Python。托盘自启的 plist 会写死这个解释器路径，
  venv 坏了自启就静默失效——所以默认推荐装纯标准库的定时任务。
- **截图验证 GUI**：按坐标 `screencapture -R` 常拍到别的窗口；用窗口 ID
  （`CGWindowListCopyWindowInfo` → `screencapture -l <id>`）才可靠。
  控件「存在且 mapped」不等于「画得出来」，必须看像素。

## 运行时数据位置

- 源码运行：数据在脚本旁边
- 打包成 `.app`：`~/Library/Application Support/SEU-Net-AutoLogin/`
  （放 app 包里的话升级就被替换，账号密码会丢）
- 日志按大小轮转，总占用上限 `(log_backup_count+1) × log_max_kb`
- 保存配置前自动留 `config.json.bak.1/2/3`
