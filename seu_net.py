#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""东南大学校园网自动认证（macOS / Windows 通用，单文件，无第三方依赖）

一个脚本，用子命令切换功能：

    python3 seu_net.py                 # 前台认证（等到成功或超时）
    python3 seu_net.py login --once    # 只试一轮
    python3 seu_net.py status          # 只看当前状态，不改网络
    python3 seu_net.py logout          # 让本机下线
    python3 seu_net.py gui             # 打开小窗口
    python3 seu_net.py init            # 首次配置向导
    python3 seu_net.py install         # 装成开机自启（launchd / 任务计划）
    python3 seu_net.py uninstall       # 卸载自启
    python3 seu_net.py doctor          # 环境自检（出问题先跑这个）

认证原理：校园门户是 Drcom。直连门户后用 PORTAL 的 `?c=ACSetting&a=Login` 提交
`DDDDD`（账号）/ `upass`（密码）。门户自己的返回只当参考，**最终以外网是否真的通为准**。

账号写法随网络环境不同（宿舍走运营商宽带、教室走校园网），程序按下面的顺序挑：
  1. 上次在这个网络里成功过的那份配置（记在 state.json）
  2. 配置里写了 SSID、且和当前 SSID 对得上的那份（拿不到 SSID 时跳过这一步）
  3. 其余配置按 priority 从小到大依次试
每份配置都真的提交一次，外网通了才认；同一个网络下次直接命中第 1 步。
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib import parse as urlparse
from urllib import request as urlrequest

APP_NAME = "seu-net-autologin"
VERSION = "2.0"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _data_dir():
    """配置、日志、状态放哪儿。

    优先级从高到低：
    1. 环境变量 SEU_DATA_DIR —— 测试时指定，绝不碰真实数据
    2. 脚本自己所在目录已经有 config.json —— 说明这是一套独立的部署/测试环境，
       配置就该跟脚本放一起。这条也让「拷一份到临时目录做测试」天然隔离。
    3. macOS 的 ~/Library/Application Support/（.app 升级不丢）
       —— 但只有那里真的有配置时才用它，避免源码运行和打包版各存一份
    4. 脚本旁边 / 可执行文件旁边
    """
    override = os.environ.get("SEU_DATA_DIR")
    if override:
        try:
            os.makedirs(override, exist_ok=True)
            return override
        except OSError:
            pass

    script_dir = os.path.dirname(os.path.abspath(__file__))
    if not getattr(sys, "frozen", False) and os.path.exists(
            os.path.join(script_dir, "config.json")):
        return script_dir

    candidates = []
    if IS_MAC:
        candidates.append(os.path.expanduser(
            "~/Library/Application Support/SEU-Net-AutoLogin"))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        candidates.append(script_dir)

    # 已经有配置的目录优先，避免同一台机器上两份配置来回打架
    for path in candidates:
        if os.path.exists(os.path.join(path, "config.json")):
            return path

    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return os.path.dirname(os.path.abspath(sys.executable
                                           if getattr(sys, "frozen", False)
                                           else __file__))


BASE_DIR = _data_dir()

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")


def resource_path(name):
    """找一个随程序发布的只读资源（比如图标）。

    打包后这些文件被解到 sys._MEIPASS 指向的临时目录，源码运行时就在脚本旁边。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

# GUI 子进程启动控制台程序时不要弹窗口（Windows）
NO_WINDOW = 0x08000000 if IS_WIN else 0


# ============================================================================
# 默认配置
# ============================================================================

DEFAULT_CONFIG = {
    "account": "",
    "password": "",

    # 场景 = 一个地方的完整配置：连哪个 Wi-Fi、用哪个账号、走哪家运营商。
    # 界面上开关就是按这里的 label 生成的，点一下就连 Wi-Fi + 认证。
    # 实际提交的账号 = account_prefix + account + account_suffix。
    # 程序按「上次成功的 → 场景匹配的 → priority」顺序试，写错的会自动跳过。
    "profiles": [
        {
            "key": "dorm",
            "label": "宿舍",
            "wifi": "SEU-ISP",
            "name": "宿舍 · 运营商宽带",
            "ssids": ["SEU-ISP"],
            "account_prefix": "",
            "account_suffix": "@cmcc",
            "priority": 10,
            "note": "移动 @cmcc / 电信 @dx / 联通 @lt",
        },
        {
            "key": "classroom",
            "label": "教室",
            "wifi": "SEU-WLAN",
            "name": "教室 · 校园网",
            "ssids": ["SEU-WLAN"],
            "account_prefix": ",0,",
            "account_suffix": "",
            "priority": 20,
            "note": "账号带 ,0, 前缀、不带运营商后缀",
        },
    ],

    # 门户探测：按顺序试，谁回 Drcom 页面就用谁。{ip} 会被替换成本机 IP
    "portal_urls": [
        "http://w.seu.edu.cn/",
        "http://10.80.128.2/",
        "http://10.9.10.100/",
    ],
    # 找到门户后，登录接口按 scheme/port 拼：http→801、https→802
    "portal_login_path": "/eportal/",
    "portal_logout_path": "/eportal/",
    # 状态查询走 80 端口的 drcom 内核接口（和上面不是同一个端口）
    "portal_status_path": "/drcom/chkstatus?callback=dr1002",

    # 外网校验：任何一条通就算通（不同网络环境能出去的地址可能不一样）
    "connectivity_checks": [
        {"url": "http://www.msftconnecttest.com/connecttest.txt", "expect": "Microsoft Connect Test"},
        {"url": "http://connect.rom.miui.com/generate_204", "expect": "", "status": 204},
    ],
    "connectivity_grace_seconds": 2,
    "connectivity_attempts": 3,

    "request_timeout_seconds": 4,
    "login_timeout_seconds": 12,
    "retry_interval_seconds": 10,
    "fast_retry_seconds": 2,
    "fast_retry_window_seconds": 120,
    "max_wait_seconds": 300,

    # 日志：达到上限就轮转成 .1/.2/…，最多留 log_backup_count 份后删最旧的
    "log_file": "login.log",
    "log_max_kb": 512,
    "log_backup_count": 3,

    # 完全没网时，按这个顺序主动去连这些无线网络（只在「一个网络都没有」时动手）
    "wifi_auto_connect": True,
    "wifi_order": ["SEU-ISP", "SEU-WLAN"],
    "wifi_interface": "",
    "wifi_connect_after_seconds": 8,
    "wifi_connect_interval_seconds": 30,
}


# ============================================================================
# 配置读写（支持 // 和 /* */ 注释）
# ============================================================================

def strip_json_comments(text):
    """剥掉 JSONC 注释，字符串里的 // 不受影响。"""
    out = []
    i, n = 0, len(text)
    in_str = escaped = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，避免改到默认值
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
            raw = fh.read()
        user = json.loads(strip_json_comments(raw))
        if isinstance(user, dict):
            # profiles 是整体替换语义：自己写了就完全按自己写的来
            if "profiles" in user and not isinstance(user["profiles"], list):
                del user["profiles"]
            cfg.update(user)
    except FileNotFoundError:
        cfg["_config_error"] = "没有 config.json（先跑 init 或 gui 里的设置）"
    except Exception as exc:
        cfg["_config_error"] = "%s: %s" % (exc.__class__.__name__, exc)

    # 兼容旧配置：没有 key 的 profile 按顺序补 dorm / classroom
    fallback_keys = ["dorm", "classroom", "hotspot"]
    used = set()
    for index, profile in enumerate(cfg.get("profiles") or []):
        if not isinstance(profile, dict):
            continue
        if not profile.get("key"):
            for candidate in fallback_keys:
                if candidate not in used:
                    profile["key"] = candidate
                    profile.setdefault("label", {"dorm": "宿舍", "classroom": "教室"}.get(candidate, candidate))
                    break
        used.add(profile.get("key"))
        if not profile.get("wifi"):
            ssids = profile.get("ssids") or []
            if ssids:
                profile["wifi"] = ssids[0]
    return cfg


def save_config(cfg):
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    tmp = CONFIG_PATH + ".tmp"
    # config.json 里是明文密码，先按 0600 建临时文件，再原子替换，
    # 避免别的本地用户读到，也避免替换过程中出现半写的配置。
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    _backup_existing_config()
    os.replace(tmp, CONFIG_PATH)


CONFIG_BACKUP_COUNT = 3


def _backup_existing_config():
    """覆盖前先把现有配置留一份 .bak.N。

    账号密码是手填的、丢了很烦（而且它不在 git 里，误删就找不回来）。
    保存新配置前自动留 3 份历史，什么时候都能翻回去。
    """
    if not os.path.exists(CONFIG_PATH):
        return
    try:
        oldest = "%s.bak.%d" % (CONFIG_PATH, CONFIG_BACKUP_COUNT)
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(CONFIG_BACKUP_COUNT - 1, 0, -1):
            src = "%s.bak.%d" % (CONFIG_PATH, index)
            if os.path.exists(src):
                os.replace(src, "%s.bak.%d" % (CONFIG_PATH, index + 1))
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak.1")
        os.chmod(CONFIG_PATH + ".bak.1", 0o600)
    except OSError:
        pass


def has_credentials(cfg):
    """只要有任意一条配置同时拿得到账号和密码，就算配置可用。

    账号和密码都可以写在顶层，也可以写在 profile 里（profile 优先、缺省回退顶层），
    所以这里要按 profile 逐个判断，不能只看顶层 account。
    """
    top_account = str(cfg.get("account") or "").strip()
    top_password = str(cfg.get("password") or "").strip()
    profiles = [p for p in (cfg.get("profiles") or []) if isinstance(p, dict)] or [{}]
    for profile in profiles:
        account = str(profile.get("account") or "").strip() or top_account
        password = str(profile.get("password") or "").strip() or top_password
        if account and password:
            return True
    return False


# ============================================================================
# 状态记忆（哪个网络用过哪份配置）
# ============================================================================

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    # 用带 pid 的临时名 + 原子替换：launchd 每分钟一个进程、GUI 也可能同时开，
    # 固定叫 state.json.tmp 的话两边会互相覆盖半成品。
    tmp = "%s.%d.tmp" % (STATE_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ============================================================================
# 日志：按大小轮转，保留固定份数，杜绝无限膨胀
# ============================================================================

def _lock_file(handle):
    """给文件加排他锁（阻塞）。Windows 用 msvcrt，类 Unix 用 fcntl。"""
    if IS_WIN:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle):
    if IS_WIN:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Logger(object):
    """写 <log_file>，超限就轮转；同时按需打印到控制台。

    老版本每写一行就检查一次大小、并在超限时把整个文件读进来再写回去，
    既费 IO，几个实例并发时还会互相踩。这里改成「只在跨过阈值时轮转一次」，
    并用 .1/.2/… 备份，最多留 log_backup_count 份，总占用有硬上限。
    """

    def __init__(self, cfg, verbose=True):
        name = cfg.get("log_file") or "login.log"
        # 日志里有账号和网络信息，.gitignore 按 *.log* 兜底。为了让这份兜底始终有效，
        # 强制日志以 .log 结尾——用户写 run.txt 也不会在仓库里留下可能被误提交的文件。
        if not name.endswith(".log"):
            name += ".log"
        self.path = os.path.join(BASE_DIR, name)
        self.max_bytes = max(16, int(cfg.get("log_max_kb", 512))) * 1024
        self.backups = max(0, int(cfg.get("log_backup_count", 3)))
        self.verbose = verbose
        self._lock = threading.RLock()
        self._size = self._current_size()
        self._last_line = ""
        self._repeat = 0
        self._repeat_reported = 0.0

    def _current_size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _truncate(self):
        try:
            with open(self.path, "w", encoding="utf-8"):
                pass
            self._size = 0
        except OSError:
            pass

    def _rotate(self):
        """login.log → login.log.1 → … → login.log.N，多出来的删掉。"""
        try:
            if self.backups <= 0:
                self._truncate()
                return
            oldest = "%s.%d" % (self.path, self.backups)
            if os.path.exists(oldest):
                os.remove(oldest)
            for index in range(self.backups - 1, 0, -1):
                src = "%s.%d" % (self.path, index)
                if os.path.exists(src):
                    os.replace(src, "%s.%d" % (self.path, index + 1))
            os.replace(self.path, self.path + ".1")
            self._size = 0
        except OSError:
            # 轮转失败也不能让日志把程序拖死：直接把当前文件截断
            self._truncate()

    def __call__(self, message):
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
        with self._lock:
            now = time.time()
            # 同一条消息反复出现时（典型：断网时每 10 秒一条「未发现门户」）
            # 不再逐条写盘，只累计次数，每分钟补一行汇总。
            if message == self._last_line:
                self._repeat += 1
                if now - self._repeat_reported >= 60:
                    self._repeat_reported = now
                    self.write_locked("%s  （上一条消息又重复了 %d 次）"
                                      % (time.strftime("%Y-%m-%d %H:%M:%S"), self._repeat))
                    self._repeat = 0
                    if self.verbose:
                        self._print(line)
                return
            if self._repeat:
                self.write_locked("%s  （上一条消息共重复 %d 次）"
                                  % (time.strftime("%Y-%m-%d %H:%M:%S"), self._repeat))
                self._repeat = 0
            self._last_line = message
            self._repeat_reported = now
            self.write_locked(line)
        if self.verbose:
            self._print(line)

    def _write(self, line):
        try:
            # 别的进程可能刚写过或轮转过，缓存的大小会过时，写之前按磁盘实际情况校正
            self._size = self._current_size()
            if self._size >= self.max_bytes:
                self._rotate()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._size += len(line.encode("utf-8")) + 1
        except OSError:
            pass

    def write_locked(self, line):
        """跨进程安全地写一行（轮转 + 追加都在锁内）。

        launchd 每 60 秒起一个独立进程、GUI 也可能同时开着一个，
        只靠 threading.RLock 拦不住进程间的竞争：A 正在把 login.log 改名，
        B 同时在往里追加，那几行就丢了。这里加操作系统级的文件锁。
        """
        lock_path = self.path + ".lock"
        handle = None
        try:
            handle = open(lock_path, "a+")
            _lock_file(handle)
        except OSError:
            handle = None
        try:
            self._write(line)
        finally:
            if handle is not None:
                try:
                    _unlock_file(handle)
                except Exception:
                    pass
                handle.close()

    @staticmethod
    def _print(line):
        try:
            if sys.stdout is not None:
                print(line, flush=True)
        except Exception:
            pass

    def tail(self, lines=200):
        """给 GUI 看的日志尾巴（含上一个轮转文件，便于看刚发生的失败）。"""
        out = []
        for path in [self.path + ".1", self.path]:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    out.extend(fh.read().splitlines())
            except OSError:
                continue
        return out[-lines:]

    def total_bytes(self):
        total = self._current_size()
        for index in range(1, self.backups + 1):
            try:
                total += os.path.getsize("%s.%d" % (self.path, index))
            except OSError:
                pass
        return total


# ============================================================================
# 单实例锁
# ============================================================================

LOCK_PATH = os.path.join(BASE_DIR, "login.lock")


class InstanceLock(object):
    """同一时刻只让一个实例真的去认证。

    为什么必须有：开机自启的任务每 60 秒起一个进程，而你可能同时开着图形界面点
    「立即认证」。两边各提交一次认证，会互相把对方的会话踢掉，日志也会写乱。
    这里用操作系统级的文件锁（不是 pid 文件那种有竞态的写法）——
    进程退出（哪怕崩溃）时内核会自动释放，不会留下死锁。
    """

    def __init__(self, path=None):
        self.path = path or LOCK_PATH
        self._handle = None

    def acquire(self):
        """拿到锁返回 True，已有别的实例在跑返回 False。"""
        try:
            handle = open(self.path, "a+")
        except OSError:
            # 锁文件都建不了（目录只读之类）：不阻塞认证，但记下来
            return True
        try:
            if IS_WIN:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            return False
        # 把当前进程信息写进去，方便排查「是谁占着锁」
        try:
            handle.seek(0)
            handle.truncate()
            handle.write("pid=%d\nstart=%s\n" % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
            handle.flush()
        except OSError:
            pass
        self._handle = handle
        return True

    def release(self):
        if self._handle is None:
            return
        try:
            if IS_WIN:
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._handle.close()
        except Exception:
            pass
        self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_exc):
        self.release()
        return False


def lock_holder():
    """锁现在被谁占着（给 status / doctor 用）。返回说明文字，没人占返回空串。"""
    if not os.path.exists(LOCK_PATH):
        return ""
    try:
        with open(LOCK_PATH, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read().strip().replace("\n", "  ")
    except OSError:
        return ""
    # 试着拿一下：能拿到就说明现在没人在跑（锁文件只是残留）
    probe = InstanceLock()
    if probe.acquire():
        probe.release()
        return ""
    return content


# ============================================================================
# 平台层：SSID / IP / 网关 / 连网
# ============================================================================

def run(cmd, timeout=20, encoding="utf-8"):
    """跑个命令拿 stdout；失败返回空串，绝不抛异常。"""
    return run_result(cmd, timeout=timeout, encoding=encoding)[1]


def run_result(cmd, timeout=20, encoding="utf-8"):
    """跑个命令，返回 (退出码, stdout, stderr)。

    需要判断成败时用这个，别用 run()——它只给 stdout，失败和「命令成功但没有输出」
    看起来一模一样，会把失败的安装/连网当成成功。
    """
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=NO_WINDOW,
        )
        return (proc.returncode,
                proc.stdout.decode(encoding, "replace"),
                proc.stderr.decode(encoding, "replace"))
    except Exception as exc:
        return (-1, "", "%s: %s" % (exc.__class__.__name__, exc))


def wifi_device():
    """无线网卡名。mac 一般是 en0，Windows 是 WLAN。"""
    if IS_MAC:
        out = run(["networksetup", "-listallhardwareports"])
        match = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\w+)", out)
        return match.group(1) if match else "en0"
    return ""


def current_ssid(cfg):
    """当前连着的无线网络名。拿不到（系统没授权定位）时返回空串，调用方需能容忍。"""
    if IS_MAC:
        device = cfg.get("wifi_interface") or wifi_device()
        out = run(["networksetup", "-getairportnetwork", device])
        match = re.search(r"Current (?:Wi-Fi|AirPort) Network:\s*(.+?)\s*$", out, re.M)
        if match and match.group(1):
            return match.group(1).strip()
        # 新版 macOS 未授予定位权限时这里会是字面量 <redacted>，不要当成网络名
        out = run(["ipconfig", "getsummary", device])
        match = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.M)
        if match:
            value = match.group(1).strip()
            if value and value != "<redacted>":
                return value
        return ""
    # Windows：netsh 需要管理员，Get-NetConnectionProfile 普通权限即可
    out = run(["netsh", "wlan", "show", "interfaces"], encoding="gbk")
    match = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.M)
    if match:
        return match.group(1).strip()
    out = run(["powershell", "-NoProfile", "-Command",
               "(Get-NetConnectionProfile | Where-Object {$_.InterfaceAlias -match 'Wi-Fi|WLAN'} | Select-Object -First 1).Name"])
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def primary_ip():
    """访问外网时本机用的源地址（不发包，只让系统选路）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2)
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        sock.close()


def default_gateway():
    if IS_MAC:
        out = run(["route", "-n", "get", "default"])
        match = re.search(r"gateway:\s*(\S+)", out)
        return match.group(1) if match else ""
    out = run(["powershell", "-NoProfile", "-Command",
               "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Select-Object -First 1).NextHop"])
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def network_fingerprint(cfg, ssid=""):
    """给「这个网络」起个稳定的名字，用来记住哪份配置在这里能用。

    优先用 SSID；拿不到 SSID（macOS 未授权定位）就退回网关 + 本机网段。
    """
    if ssid:
        return "ssid:" + ssid.lower()
    gateway = default_gateway()
    ip = primary_ip()
    if not gateway and not ip:
        return "unknown"
    prefix = re.sub(r"\.\d+$", "", ip) if ip else ""
    return "gw:%s|net:%s" % (gateway or "?", prefix or "?")


def wifi_offline_hint():
    """完全没有网络时的提示语，各平台不一样。"""
    if IS_MAC:
        return "检查菜单栏 Wi-Fi 是否已连上 SEU-WLAN / SEU-ISP"
    return "检查 Windows 是否已连上 SEU-WLAN / SEU-ISP"


def run_wifi_connect(cfg, ssid, wait_seconds=20):
    """连指定 Wi-Fi，并**等网络真的恢复**再返回。

    坑：切换 Wi-Fi 的那几秒网络是断的，此时探测门户必然全失败。
    之前直接 sleep(3) 就去探测，结果每次都报「连不上校园 Wi-Fi」，
    根本没走到认证——用户看到的就是「程序没自己认证，还弹出登录页让我手填」。

    所以这里等到「本机拿到 IP 且门户可达」为止，最多等 wait_seconds 秒。
    """
    if not ssid:
        return False

    if IS_MAC:
        device = cfg.get("wifi_interface") or wifi_device()
        run(["networksetup", "-setairportnetwork", device, ssid], timeout=25)
    else:
        device = cfg.get("wifi_interface") or ""
        cmd = ["netsh", "wlan", "connect", "name=%s" % ssid]
        if device:
            cmd.append("interface=%s" % device)
        run(cmd, timeout=25, encoding="gbk")

    # 轮询等网络恢复：先有 IP，再能访问门户
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(2)
        if not primary_ip():
            continue
        if _portal_reachable(cfg):
            return True
    # 等不到也不代表失败——有些网络拿到 IP 但要认证后才能访问门户，
    # 交给调用方的认证流程判断
    return bool(primary_ip())


def join_wifi(ssid, cfg):
    """主动连指定无线网络，并**确认真的连上了**。

    坑：networksetup -setairportnetwork 无论成功失败都返回 0、通常没有输出；
    而且连不上时会留在原来那个 Wi-Fi 上，原网络照样能访问门户——
    只用「门户可达」判断会把失败当成成功（实测过，会谎报「已连上教室」）。

    所以这里先记录切换前的网络指纹，切换后要求指纹发生变化，
    再加上门户可达，两个条件都满足才算成功。
    """
    if not ssid:
        return False

    before = _wifi_fingerprint(cfg)

    if IS_MAC:
        device = cfg.get("wifi_interface") or wifi_device()
        run(["networksetup", "-setairportnetwork", device, ssid], timeout=25)
    else:
        device = cfg.get("wifi_interface") or ""
        cmd = ["netsh", "wlan", "connect", "name=%s" % ssid]
        if device:
            cmd.append("interface=%s" % device)
        run(cmd, timeout=25, encoding="gbk")

    # 关联需要几秒，最多等 15 秒
    deadline = time.time() + 15
    portal_ok_since = None
    while time.time() < deadline:
        time.sleep(2)
        after = _wifi_fingerprint(cfg)
        changed = after and after != before
        if _portal_reachable(cfg):
            # 门户通了，还要确认网络真的换了（防止「留在原网络」被误判）
            if changed:
                return True
            if portal_ok_since is None:
                portal_ok_since = time.time()
            # 网络指纹拿不到时（macOS 读不到 SSID）只能靠 BSSID 变化判断
            elif time.time() - portal_ok_since > 6:
                return _bssid_changed(before, after) or not before
    return False


def scene_from_online_account(cfg, uid):
    """用门户回显的在线账号反推当前在用哪个场景。

    macOS 读不到 SSID / BSSID（未授权定位时全是 <redacted>），
    但门户会告诉我们「这个 IP 上现在是哪个账号在线」——比 SSID 更准。
    返回场景 key，对不上返回空串。
    """
    uid = str(uid or "").strip()
    if not uid:
        return ""
    # 门户回显的 uid 可能带后缀（213253274@unicom）、可能带前缀（,0,213252649）
    bare_uid = uid.split("@")[0]
    for profile in cfg.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        key = profile.get("key")
        if not key:
            continue
        account = str(profile.get("account") or cfg.get("account") or "").strip()
        if not account:
            continue
        if account == bare_uid or account in uid:
            return key
    return ""


def resolve_current_scene(cfg):
    """当前在用的场景：优先用门户回显的账号反推，其次用手动/记忆的。"""
    timeout = float(cfg.get("request_timeout_seconds", 4))
    portal, _info = find_portal(make_opener(), cfg, timeout)
    if portal:
        online, _detail, uid = query_online(portal, cfg, timeout)
        if online:
            matched = scene_from_online_account(cfg, uid)
            if matched:
                _remember_scene(matched)
                return matched
    return active_scene(cfg)


def _wifi_fingerprint(cfg):
    """当前 Wi-Fi 的指纹（BSSID 优先，拿不到 SSID 时的替代）。"""
    device = cfg.get("wifi_interface") or wifi_device()
    out = run(["ipconfig", "getsummary", device])
    match = re.search(r"^\s*BSSID\s*:\s*(.+?)\s*$", out, re.M)
    if not match:
        return ""
    value = match.group(1).strip()
    return "" if value == "<redacted>" else value


def _bssid_changed(before, after):
    return bool(before) and bool(after) and before != after


def _portal_reachable(cfg):
    """能不能访问到校园网认证门户（说明已关联到校园 Wi-Fi）。"""
    opener = make_opener()
    timeout = min(3.0, float(cfg.get("request_timeout_seconds", 4)))
    for url in cfg.get("portal_urls") or DEFAULT_CONFIG["portal_urls"]:
        try:
            status, raw = http_request(opener, url, timeout=timeout)
        except Exception:
            continue
        if status and any(m in decode_body(raw).lower() for m in PORTAL_MARKERS):
            return True
    return False


# ============================================================================
# HTTP 与门户
# ============================================================================

def make_opener():
    # 传空 dict 的 ProxyHandler = 明确不走系统代理，直连校园门户
    return urlrequest.build_opener(urlrequest.ProxyHandler({}))


def http_request(opener, url, body=None, timeout=6, user_agent=None):
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0 (compatible; %s/%s)" % (APP_NAME, VERSION),
        "Accept": "*/*",
        "Connection": "close",
    }
    data = None
    if body is not None:
        data = urlparse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urlrequest.Request(url, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read()


def decode_body(raw):
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", "replace")


PORTAL_MARKERS = ("dr.com", "drcom", "webloginid")


def find_portal(opener, cfg, timeout):
    """依次探测门户地址，返回 (base_url 说明, 备注)。找不到时 base 为 None。"""
    notes = []
    for url in cfg.get("portal_urls") or DEFAULT_CONFIG["portal_urls"]:
        try:
            status, raw = http_request(opener, url, timeout=timeout)
        except Exception as exc:
            notes.append("%s → %s" % (url, exc.__class__.__name__))
            continue
        text = decode_body(raw)
        if any(marker in text.lower() for marker in PORTAL_MARKERS):
            parts = urlparse.urlsplit(url)
            # 登录/注销走 eportal 的 801(http)/802(https)；状态查询走 80 端口的 /drcom/ 内核接口，
            # 两者端口不同，不能共用一个基地址（实测 801 上 /drcom/chkstatus 返回 404）。
            api_scheme = "https" if parts.port == 802 else "http"
            api_port = 802 if parts.port == 802 else 801
            return {
                "portal": url,
                "base": "%s://%s:%d" % (api_scheme, parts.hostname, api_port),
                "status_base": "%s://%s" % (parts.scheme, parts.hostname),
                "host": parts.hostname,
            }, "HTTP %s，识别为 Drcom 门户" % status
        notes.append("%s → HTTP %s，不像 Drcom 门户（%s）"
                     % (url, status, re.sub(r"\s+", " ", text.strip())[:60]))
    return None, "；".join(notes) if notes else "没有可用的门户地址"


def build_login_url(portal, cfg, creds):
    path = cfg.get("portal_login_path") or "/eportal/"
    return "%s%s?%s" % (portal["base"], path, urlparse.urlencode(_login_params(portal, cfg, creds)))


def _login_params(portal, cfg, creds):
    ip = primary_ip()
    return {
        "c": "ACSetting", "a": "Login", "protocol": "http:",
        "hostname": portal["host"], "iTermType": "1",
        "wlanuserip": ip, "wlanacip": "", "wlanacname": "",
        "mac": "000000000000", "ip": ip, "queryString": "",
        "enpassword": "", "DDDDD": creds["login_name"], "upass": creds["password"],
        "R1": "0", "R2": "", "R3": "0", "R6": "0", "para": "00",
        "0MKKey": "123456", "v": str(int(time.time() * 1000)), "ver": "1.0", "v6ip": "",
    }


def build_logout_url(portal, cfg):
    ip = primary_ip()
    params = {
        "c": "ACSetting", "a": "Logout", "protocol": "http:",
        "hostname": portal["host"], "iTermType": "1",
        "wlanuserip": ip, "wlanacip": "", "wlanacname": "",
        "mac": "000000000000", "ip": ip, "ver": "1.0", "v6ip": "",
    }
    path = cfg.get("portal_logout_path") or "/eportal/"
    return "%s%s?%s" % (portal["base"], path, urlparse.urlencode(params))


def query_online(portal, cfg, timeout):
    """问门户本机是否在线。返回 (在线与否, 说明, uid)。

    chkstatus 在未认证时会被劫持成登录页，解析不出 JSON，这时就当「不在线」。
    """
    url = portal["status_base"] + (cfg.get("portal_status_path") or DEFAULT_CONFIG["portal_status_path"])
    try:
        _status, raw = http_request(make_opener(), url, timeout=timeout)
    except Exception as exc:
        return False, "%s: %s" % (exc.__class__.__name__, exc), ""
    text = decode_body(raw)
    data = _extract_json(text)
    if not data:
        return False, "门户状态查询被拦截（未认证）", ""
    if str(data.get("result")) == "1":
        return True, "门户显示在线", str(data.get("uid") or "")
    return False, "门户显示不在线", ""


def _extract_json(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


AUTH_FAIL_KEYS = (
    "用户不存在", "账号不存在", "密码错误", "用户名或密码错误", "认证失败", "被拒绝",
    "passerror", "usererror", "notexist", "authentication fail", "auth fail", "denied",
)


# 门户在这种情况下返回「失败」，但其实不是账号密码的问题：
# IP 上已经有一个会话了（通常就是自己正连着的那个）。不该判成认证被拒。
ALREADY_ONLINE_KEYS = (
    "终端ip已在线", "已在线", "已经在线", "ip已在线",
    "you are already online", "already online",
    # Drcom 在「账号已有会话 / 在线数超限」时回这个，不是密码错
    "welcome to drcom system",
    "在线数已达最大允许登录数",
    "已达到最大允许登录数",
)


def classify_login(text):
    """把门户的登录返回分成 ok / online / authfail / unknown。

    门户的两种返回（都实测过）：
      成功   Msg=0;...                                 或引用 Dr.COMWebLoginID_3.htm
      失败   Msg=01;msga='用户名或密码错误！'; Login fail.
      已在线 Msg=01;msga='终端IP已在线！'; Login fail.

    判定顺序很重要：先看 Msg 码，再看文案。只看文案会漏——
    「终端IP已在线！」里没有任何失败关键词，但 Msg=01 明确是没成功。
    """
    body = text or ""
    lowered = body.lower()
    message = _portal_message(body)

    # 1) 先认「已经在线」——它同时满足「失败」和「不该重试」两个特征
    if any(key in (message + " " + body).lower() for key in ALREADY_ONLINE_KEYS):
        return "online", message or "终端已在线"

    # 2) 看门户给的 Msg 码：0 成功，非 0 一律失败
    code_match = re.search(r"msg\s*=\s*'?(\d+)", lowered)
    if code_match:
        if code_match.group(1) == "0":
            return "ok", message or "门户返回 Msg=0"
        if any(key in lowered for key in AUTH_FAIL_KEYS):
            return "authfail", message or ("Msg=%s" % code_match.group(1))
        # Msg 非 0 但文案里没有明确的账号错误：算失败，但不该当「密码错」直接放弃
        return "authfail", message or ("Msg=%s" % code_match.group(1))

    # 3) 没有 Msg 码时看页面特征
    if "webloginid_3" in lowered:
        return "ok", message or "门户返回成功页"
    if any(key in lowered for key in AUTH_FAIL_KEYS):
        return "authfail", message or "认证被拒绝"
    if "webloginid_2" in lowered:
        return "authfail", message or "门户返回失败页"

    uid = re.search(r"uid\s*=\s*'([^']*)'", body)
    if uid and uid.group(1):
        return "ok", "门户回显账号 %s" % uid.group(1)
    return "unknown", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()[:100]


def _portal_message(body):
    match = re.search(r"msga\s*=\s*'([^']*)'", body or "")
    if match and match.group(1):
        return match.group(1)
    match = re.search(r"Msg\s*=\s*'?([^;']*)", body or "")
    return ("Msg=%s" % match.group(1)) if match else ""


# ============================================================================
# 外网校验：门户说什么都不算数，真连出去才算
# ============================================================================

def connectivity_ok(cfg, timeout):
    checks = cfg.get("connectivity_checks") or DEFAULT_CONFIG["connectivity_checks"]
    details = []
    for check in checks:
        url = check.get("url")
        if not url:
            continue
        try:
            status, raw = http_request(make_opener(), url, timeout=timeout)
        except Exception as exc:
            details.append("%s → %s" % (url, exc.__class__.__name__))
            continue
        expect_status = check.get("status")
        if expect_status is not None and status == expect_status:
            return True, "HTTP %s（%s）" % (status, url)
        expect = check.get("expect") or ""
        if status == 200 and expect:
            text = decode_body(raw)
            if expect in text:
                return True, "HTTP %s，内容匹配" % status
            if any(marker in text.lower() for marker in PORTAL_MARKERS):
                details.append("%s → 被门户劫持" % url)
                continue
            details.append("%s → 内容不符" % url)
            continue
        details.append("%s → HTTP %s" % (url, status))
    return False, "；".join(details) or "没有配置校验地址"


def wait_connectivity(cfg, log, timeout):
    total = max(1, int(cfg.get("connectivity_attempts", 3)))
    grace = max(0.0, float(cfg.get("connectivity_grace_seconds", 2)))
    for index in range(1, total + 1):
        ok, info = connectivity_ok(cfg, timeout)
        if ok:
            log("    外网校验通过（%s）" % info)
            return True
        log("    第 %d/%d 次外网校验未通过：%s" % (index, total, info))
        if index < total:
            time.sleep(grace)
    return False


# ============================================================================
# 账号候选：按环境挑配置
# ============================================================================

def _profile_login_name(profile, cfg):
    prefix = profile.get("account_prefix")
    suffix = profile.get("account_suffix")
    account = str(profile.get("account") or cfg.get("account") or "")
    return "%s%s%s" % (
        "" if prefix is None else str(prefix),
        account,
        (str(cfg.get("account_suffix") or "") if suffix is None else str(suffix)),
    )


def _profile_credentials(profile, cfg):
    return {
        "login_name": _profile_login_name(profile, cfg),
        "password": str(profile.get("password") or cfg.get("password") or ""),
    }


def _profile_label(profile, index):
    name = profile.get("name") or profile.get("note") or ""
    return name or "配置%d" % (index + 1)


def candidate_profiles(cfg, ssid, fingerprint):
    """排出这次要依次尝试的配置顺序，并说明每条为什么排在这里。"""
    profiles = [p for p in (cfg.get("profiles") or []) if isinstance(p, dict)]
    if not profiles:
        profiles = [{}]  # 没有 profiles 就用顶层 account/password 裸试一次

    remembered = (load_state().get("profiles") or {}).get(fingerprint)
    ordered, used = [], set()

    def add(index, reason):
        if index in used:
            return
        used.add(index)
        ordered.append((index, profiles[index], reason))

    # 1) 这个网络上次成功过的那份
    if remembered is not None and 0 <= int(remembered) < len(profiles):
        add(int(remembered), "上次在这个网络成功过")

    # 2) SSID 对得上的（拿不到 SSID 时这一步自然为空）
    if ssid:
        for index, profile in enumerate(profiles):
            names = profile.get("ssids") or ([profile.get("ssid")] if profile.get("ssid") else [])
            for name in names:
                if name and str(name).lower() == ssid.lower():
                    add(index, "匹配当前网络 %s" % ssid)
                    break

    # 3) 其余按 priority（小的优先），再按配置里的顺序
    rest = [(index, profile) for index, profile in enumerate(profiles) if index not in used]
    rest.sort(key=lambda item: (int(item[1].get("priority", 100)), item[0]))
    for index, profile in rest:
        add(index, "按优先级依次尝试")
    return ordered


def remember_profile(fingerprint, index):
    state = load_state()
    profiles = state.setdefault("profiles", {})
    profiles[fingerprint] = index
    state["last_success"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)


HEALTHY_HEARTBEAT_SECONDS = 3600


def note_healthy(log, fingerprint, reason):
    """外网本来就是通的：只在「刚变通」或每满一小时时记一行。

    自启任务一分钟跑一次，每次都写「外网已通」既没信息量又把日志撑大。
    状态记在 state.json，重启也不会重复刷。
    """
    state = load_state()
    healthy = state.get("healthy") or {}
    last_at = float(healthy.get("at") or 0)
    now = time.time()
    same_network = healthy.get("fingerprint") == fingerprint
    if same_network and (now - last_at) < HEALTHY_HEARTBEAT_SECONDS:
        return
    if same_network:
        log("外网持续可用（%s）" % reason)
    else:
        log("外网已通，不需要认证（%s，%s）" % (reason, fingerprint))
    state["healthy"] = {"fingerprint": fingerprint, "at": now}
    save_state(state)


def clear_healthy():
    """从通变成不通时清掉标记，下次通了会立刻记一行。"""
    state = load_state()
    if state.pop("healthy", None) is not None:
        save_state(state)


def forget_profile(fingerprint):
    state = load_state()
    (state.get("profiles") or {}).pop(fingerprint, None)
    save_state(state)


# ============================================================================
# 认证主流程
# ============================================================================

def try_login_once(cfg, log, portal, profile, index, timeout, login_timeout):
    """用一份配置提交一次认证，并以外网是否真的通为最终判据。

    后缀被拒时会自动试别名后缀（比如配置写 @lt、门户其实认 @unicom）。
    Drcom 各校/各期版本认的后缀不一样，写死一个很容易下次就失效。
    """
    creds = _profile_credentials(profile, cfg)
    if not creds["password"]:
        log("    %s：没有密码，跳过" % _profile_label(profile, index))
        return "skip", ""
    label = _profile_label(profile, index)

    # 要试的账号写法：原样 + 别名后缀
    suffix = str(profile.get("account_suffix") or cfg.get("account_suffix") or "")
    login_names = [creds["login_name"]]
    for alias in SUFFIX_ALIASES.get(suffix, ()):
        candidate = creds["login_name"]
        if suffix and candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)] + alias
        else:
            candidate = creds["login_name"] + alias
        login_names.append(candidate)

    state, detail = "unknown", ""
    for name in login_names:
        attempt = dict(creds, login_name=name)
        url = build_login_url(portal, cfg, attempt)
        try:
            _status, raw = http_request(make_opener(), url, timeout=login_timeout)
            state, detail = classify_login(decode_body(raw))
        except Exception as exc:
            state, detail = "unknown", "%s: %s" % (exc.__class__.__name__, exc)

        log("  提交认证（%s｜账号 %s）→ %s" % (label, name, detail))
        if state != "authfail":
            break
        if name != login_names[-1]:
            log("    这个后缀被拒，换别名后缀再试")

    if state == "authfail":
        return "authfail", detail
    if state == "online":
        # 这个 IP 上已经有会话了（可能正是自己）。不算失败，也不是账号问题，
        # 让调用方去校验外网到底通不通，别把这条当「密码错」直接放弃。
        log("    门户提示该 IP 已在线，交给外网校验决定")
        if wait_connectivity(cfg, log, timeout):
            return "ok", detail
        return "stuck", detail

    if wait_connectivity(cfg, log, timeout):
        return "ok", detail
    return "neterr", detail


def _scene_keys(cfg):
    """配置里所有场景的 key，按声明顺序。"""
    return [p.get("key") for p in (cfg.get("profiles") or [])
            if isinstance(p, dict) and p.get("key")]


def active_scene(cfg):
    """当前该用哪个场景。优先用户手动选的，其次上次成功的，最后第一个。"""
    keys = _scene_keys(cfg)
    if not keys:
        return ""
    state = load_state()
    manual = state.get("manual_scene")
    if manual in keys:
        return manual
    last = state.get("last_scene")
    if last in keys:
        return last
    return keys[0]


def scenes(cfg):
    """把 profiles 里带 key 的条目当成「场景」，给界面用。"""
    current = active_scene(cfg)
    out = []
    for profile in cfg.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        key = profile.get("key")
        if not key:
            continue
        out.append({
            "key": key,
            "label": profile.get("label") or profile.get("name") or key,
            "wifi": profile.get("wifi") or (profile.get("ssids") or [""])[0],
            "name": profile.get("name") or key,
            "is_current": key == current,
        })
    return out


def find_scene(cfg, key):
    for profile in cfg.get("profiles") or []:
        if isinstance(profile, dict) and profile.get("key") == key:
            return profile
    return None


def set_manual_scene(key):
    """手动指定场景后，自动回退时优先用它。"""
    state = load_state()
    if key:
        state["manual_scene"] = key
    else:
        state.pop("manual_scene", None)
    save_state(state)


def _remember_scene(key):
    state = load_state()
    state["last_scene"] = key
    save_state(state)


def switch_scene(cfg, log, key, connect_wifi=True):
    """切到指定场景：连 Wi-Fi → 认证。返回 {"ok","code","scene","message"}。

    这是「一键切换」的核心。连 Wi-Fi 失败就直接说明，不用浪费时间走认证。
    """
    profile = find_scene(cfg, key)
    if not profile:
        return {"ok": False, "code": -1, "scene": key, "message": "没有这个场景：%s" % key}

    label = profile.get("label") or profile.get("name") or key
    wifi = profile.get("wifi") or (profile.get("ssids") or [""])[0]

    if connect_wifi and wifi:
        log("切换到 %s：连接 Wi-Fi %s" % (label, wifi))
        run_wifi_connect(cfg, str(wifi))

    # 只提交这一个场景的账号，不参与「依次试」，避免切到教室却用了宿舍的号
    instance = InstanceLock()
    if not instance.acquire():
        return {"ok": False, "code": -1, "scene": key,
                "message": "已有认证正在进行，请稍后再试"}
    try:
        code = _authenticate_scene_only(cfg, log, profile)
    finally:
        instance.release()

    ok = code == 0
    if ok:
        _remember_scene(key)
        log("已切到 %s（%s）" % (label, wifi))
    return {"ok": ok, "code": code, "scene": key,
            "message": "已连上 %s" % label if ok else "认证未成功（退出码 %s）" % code}


def _authenticate_scene_only(cfg, log, profile):
    """只用指定 profile 认证，不尝试其它配置。

    返回 0 表示「这个场景真的生效了」——不只是「网络通」，
    还要确认门户回显的账号就是这个场景的号。
    否则在教室点「宿舍」时，因为原网络本来就通，会被误判成切换成功。
    """
    timeout = float(cfg.get("request_timeout_seconds", 4))
    login_timeout = float(cfg.get("login_timeout_seconds", 12))
    expected = str(profile.get("account") or cfg.get("account") or "").strip()

    portal, info = find_portal(make_opener(), cfg, timeout)
    if not portal:
        log("未发现校园网门户（%s）：连不上校园 Wi-Fi" % info)
        return 1

    reachable, _detail = connectivity_ok(cfg, timeout)
    online, _detail2, uid = query_online(portal, cfg, timeout)

    if reachable:
        # 网络本来就通。必须确认在线账号就是这个场景的，否则说明切错网络了。
        bare_uid = str(uid or "").split("@")[0]
        if expected and bare_uid == expected:
            log("外网已通，且当前在线账号就是本场景（%s），无需重新认证" % bare_uid)
            return 0
        if not uid:
            log("外网已通，但读不到在线账号，无法确认切换成功")
            return 0
        log("外网已通但在线的是 %s，不是本场景的 %s —— 先注销再认证"
            % (bare_uid or uid, expected))
        do_logout(portal, cfg, log)
        time.sleep(2)
        online = False

    if online:
        log("门户显示在线但外网不通，先注销再重新认证")
        do_logout(portal, cfg, log)
        time.sleep(2)

    index = (cfg.get("profiles") or []).index(profile)
    state, _detail = try_login_once(
        cfg, log, portal, profile, index, timeout, login_timeout)
    if state == "ok":
        # 认证后再核对一次，确认门户认的就是这个号
        _ok, _d, new_uid = query_online(portal, cfg, timeout)
        bare_new = str(new_uid or "").split("@")[0]
        if expected and bare_new and bare_new != expected:
            log("认证后门户显示的账号是 %s，不是 %s" % (bare_new, expected))
            return 1
        return 0
    if state == "authfail":
        return 2
    return 1


def authenticate(cfg, log, once=False, max_wait=None):
    """完整的认证流程，返回退出码：0 成功 / 1 超时 / 2 账号被拒 / 3 配置不全。"""
    if not has_credentials(cfg):
        log("配置不完整：缺少一卡通号或密码。运行 `seu_net.py init` 补上，或打开 gui 里的设置。")
        return 3

    # 自启任务和手动操作可能撞在一起，只让一个去认证。
    # 拿不到锁说明别人正在做同一件事，这边直接退出即可（不是失败）。
    instance = InstanceLock()
    if not instance.acquire():
        holder = lock_holder()
        log("已有另一个实例正在认证%s，本次跳过"
            % (("（%s）" % holder) if holder else ""))
        return 0
    try:
        return _authenticate_locked(cfg, log, once, max_wait)
    finally:
        instance.release()


def _authenticate_locked(cfg, log, once, max_wait):
    if not has_credentials(cfg):
        log("配置不完整：缺少一卡通号或密码。运行 `seu_net.py init` 补上，或打开 gui 里的设置。")
        return 3

    timeout = float(cfg.get("request_timeout_seconds", 4))
    login_timeout = float(cfg.get("login_timeout_seconds", 12))
    interval = max(1.0, float(cfg.get("retry_interval_seconds", 10)))
    fast_interval = max(0.5, float(cfg.get("fast_retry_seconds", 2)))
    fast_window = max(0.0, float(cfg.get("fast_retry_window_seconds", 120)))
    limit = float(cfg.get("max_wait_seconds", 300) if max_wait is None else max_wait)

    ssid = current_ssid(cfg)
    fingerprint = network_fingerprint(cfg, ssid)
    reason = "网络 %s" % ssid if ssid else "SSID 不可读（未授权定位），用网关识别"

    # 已经能上网就什么都不做。定时任务每分钟跑一次，不先短路的话
    # 每次都会重新提交认证、把刚稳定的会话踢掉。
    reachable, _detail = connectivity_ok(cfg, timeout)
    if reachable:
        # 自启任务一分钟一次，每次都记一行「外网已通」就是纯噪音（一天 1400 行、
        # 内容完全一样）。只在状态发生变化时写，平时最多每小时留一条心跳。
        note_healthy(log, fingerprint, reason)
        return 0

    # 从「通」变成「不通」，这条值得记
    clear_healthy()
    log("===== 开始认证｜%s｜%s =====" % (reason, fingerprint))

    started = time.time()
    # --once：只跑一轮，把截止时间设成现在，循环末尾立刻退出
    deadline = started if once else started + limit
    attempt = 0
    last_nudge = 0.0
    while True:
        attempt += 1
        now = time.time()
        elapsed = now - started
        wait = fast_interval if elapsed < fast_window else interval

        portal, info = find_portal(make_opener(), cfg, timeout)
        if not portal:
            # 门户探不到：可能不在校园网，也可能 Wi-Fi 还没连上
            if elapsed >= float(cfg.get("wifi_connect_after_seconds", 8)):
                last_nudge = maybe_connect_wifi(cfg, log, last_nudge)
            if attempt >= 3 and attempt % 3 == 0:
                reachable, _info = connectivity_ok(cfg, timeout)
                if reachable:
                    log("第 %d 次：探测不到校园网门户，但外网是通的（当前不在校园网），本次结束" % attempt)
                    return 0
            log("第 %d 次：未发现校园网门户（%s）。%s" % (attempt, info, wifi_offline_hint()))
        else:
            # 门户说在线、外网却不通：多半是会话卡死（换了网络、AC 没放行），
            # 先注销再重新认证，否则反复登录都会被门户当成「已在线」直接返回。
            online, online_detail, _uid = query_online(portal, cfg, timeout)
            if online:
                log("门户显示在线但外网不通（%s），先注销再重新认证" % online_detail)
                do_logout(portal, cfg, log)
                time.sleep(2)

            ordered = candidate_profiles(cfg, current_ssid(cfg), fingerprint)
            rejected = []
            skipped = 0
            stuck = 0
            for position, (index, profile, why) in enumerate(ordered, 1):
                state, detail = try_login_once(
                    cfg, log, portal, profile, index, timeout, login_timeout)
                if state == "ok":
                    remember_profile(fingerprint, index)
                    log("认证完成，外网已连通（%s）" % _profile_label(profile, index))
                    return 0
                if state == "authfail":
                    rejected.append((_profile_label(profile, index), detail))
                elif state == "skip":
                    skipped += 1
                elif state == "stuck":
                    # 门户说这个 IP 已在线、外网却不通：会话卡住了，不是账号问题。
                    # 这轮先记下，下一轮开头会先注销再重来。
                    stuck += 1
                if position < len(ordered):
                    log("    这一份没成，换下一份配置")

            # 没有任何一条配置真正提交过（全被跳过）说明配置有问题；
            # 提交过的全被门户明确拒绝说明账号密码有问题。两种都不该继续重试。
            # 但「IP 已在线」不算拒绝——那说明凭据可能没问题，只是会话卡住，
            # 应该继续走重试/注销流程，而不是报「密码错」。
            if ordered and stuck == 0 and len(rejected) + skipped == len(ordered):
                forget_profile(fingerprint)
                if rejected:
                    log("所有配置都被门户拒绝：%s" % "；".join("%s→%s" % r for r in rejected))
                    log("请检查一卡通号、密码，以及 profiles 里各条的后缀（@cmcc/@dx/@lt）")
                else:
                    log("所有配置都因为缺密码被跳过：请在 profiles 或顶层的 password 里填上密码")
                return 2
            log("第 %d 次：外网仍不通，%g 秒后重试" % (attempt, wait))

        if time.time() >= deadline:
            log("本次未完成认证（等待超时），下次触发会再试")
            return 1
        time.sleep(wait)


def maybe_connect_wifi(cfg, log, last_nudge):
    """完全没有网络时，主动去连配置里的无线网络。

    只在「一个可用网络都没有」时才动手，不会把已经能上网的机器抢过来。
    """
    if not cfg.get("wifi_auto_connect", True):
        return last_nudge
    gap = float(cfg.get("wifi_connect_interval_seconds", 30))
    now = time.time()
    if last_nudge and now - last_nudge < gap:
        return last_nudge
    if primary_ip():
        return last_nudge  # 已经有网络（哪怕还没认证），不抢
    for ssid in (cfg.get("wifi_order") or []):
        if not ssid:
            continue
        ok = join_wifi(str(ssid), cfg)
        log("    主动连接无线网络 %s：%s" % (ssid, "已发起" if ok else "未成功"))
        if ok:
            break
    return now


# ============================================================================
# 状态查询
# ============================================================================

def collect_status(cfg):
    """给 GUI / status 子命令用的状态快照，纯只读。"""
    timeout = float(cfg.get("request_timeout_seconds", 4))
    ssid = current_ssid(cfg)
    info = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "ssid": ssid,
        "ssid_readable": bool(ssid),
        "ip": primary_ip(),
        "gateway": default_gateway(),
        "fingerprint": network_fingerprint(cfg, ssid),
        "portal": "",
        "online": False,
        "online_detail": "",
        "uid": "",
        "connectivity": False,
        "connectivity_detail": "",
        "credentials": has_credentials(cfg),
        "config_error": cfg.get("_config_error", ""),
        "active_scene": active_scene(cfg),
    }
    portal, detail = find_portal(make_opener(), cfg, timeout)
    if portal:
        info["portal"] = portal["base"]
        online, online_detail, uid = query_online(portal, cfg, timeout)
        info["online"], info["online_detail"], info["uid"] = online, online_detail, uid
        # 用门户回显的账号反推当前场景，比读 SSID 可靠（macOS 读不到 SSID）
        if online and uid:
            matched = scene_from_online_account(cfg, uid)
            if matched:
                info["active_scene"] = matched
                _remember_scene(matched)
    else:
        info["online_detail"] = detail
    ok, conn_detail = connectivity_ok(cfg, timeout)
    info["connectivity"], info["connectivity_detail"] = ok, conn_detail
    return info


# ============================================================================
# 首次配置向导
# ============================================================================

# 运营商后缀。注意：不同 Drcom 门户认的后缀不一样，实测东南这家认 @unicom，
# 而 @lt 会返回 Authentication fail。所以配置里用 @unicom；
# 老配置里写了 @lt 的，认证时会自动再试一次 @unicom（见 SUFFIX_ALIASES）。
CARRIERS = {
    "1": ("@cmcc", "中国移动"),
    "2": ("@dx", "中国电信"),
    "3": ("@unicom", "中国联通"),
}

# 后缀别名：配置里写了左边的，认证时额外试右边的。
# 2026-11 实测：联通号用 @lt 报 Authentication fail，用 @unicom 才认（返回已在线）。
SUFFIX_ALIASES = {
    "@lt": ("@unicom",),
    "@unicom": ("@lt",),
}


def build_config(account, password, carrier_choice="",
                 wlan_account="", wlan_password=""):
    """按「宿舍账号 + 教室账号 + 运营商选择」生成一份完整配置。

    carrier_choice 是 "1"/"2"/"3"（明确知道哪家），留空表示不确定——
    这时三家都写上，认证时依次尝试、失败的自助跳过。

    宿舍和教室用同一个号时（大多数情况），wlan_account / wlan_password 留空，
    教室那条会自动回退到顶层的 account / password；用不同的号就把它们传进来。
    命令行向导和图形界面的设置对话框共用这一份逻辑。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["account"] = account
    cfg["password"] = password
    wlan_profile = cfg["profiles"][1]

    if carrier_choice in CARRIERS:
        suffix, carrier = CARRIERS[carrier_choice]
        isp_profiles = [{
            "name": "运营商宽带（宿舍，%s）" % carrier,
            "ssids": ["SEU-ISP"],
            "account_prefix": "",
            "account_suffix": suffix,
            "priority": 10,
            "note": "宿舍里的 SEU-ISP，走%s认证" % carrier,
        }]
    else:
        # 不确定：三家都写上。不要默认成 @cmcc——电信/联通用 @cmcc 会一直认证失败。
        isp_profiles = [{
            "name": "运营商宽带（宿舍，%s，不确定就都试）" % carrier,
            "ssids": ["SEU-ISP"],
            "account_prefix": "",
            "account_suffix": suffix,
            "priority": 10 + offset,
            "note": "不确定运营商时自动尝试；确认后可删掉用不上的两条",
        } for offset, (suffix, carrier) in enumerate(
            (CARRIERS["1"], CARRIERS["2"], CARRIERS["3"]))]

    # 教室用的是另一个号时，单独写在这个 profile 里（留空则回退到顶层的号）
    if wlan_account.strip():
        wlan_profile["account"] = wlan_account.strip()
    if wlan_password.strip():
        wlan_profile["password"] = wlan_password.strip()

    wlan_profile["priority"] = 50
    cfg["profiles"] = isp_profiles + [wlan_profile]
    return cfg


def first_run_wizard(force=False):
    """没配置时在终端里问几个问题，写出 config.json。

    注意：只有能交互的终端才走这里。双击启动（没有终端）时由 GUI 的设置对话框接手。
    """
    if not force and not (sys.stdin and sys.stdout and sys.stdout.isatty()):
        return False
    print()
    print("=" * 60)
    print(" 第一次运行，回答几个问题就能用了（回车 = 用方括号里的默认值）")
    print("=" * 60)
    try:
        account = ""
        while not account:
            account = input(" 1/4 宿舍宽带（SEU-ISP）的账号：").strip()
        password = input(" 2/4 宿舍宽带的密码：").strip()
        print(" 3/4 宿舍的运营商宽带是哪一家？")
        print("      1 = 中国移动   2 = 中国电信   3 = 中国联通")
        print("      回车 = 不确定 / 都写上，让程序自己去试")
        choice = input("      [回车 = 不确定，三家都配上]：").strip()

        # 教室（SEU-WLAN）是不是另一个号。宿舍和教室共用一个号的情况很常见，
        # 所以默认回车就是「同一个号」，只有明确回答 y 才继续问。
        print(" 4/4 教室的校园网（SEU-WLAN）用的是同一个号吗？")
        print("      回车 = 是，同一个号；输入 y = 另一个号")
        wlan_account = wlan_password = ""
        if input("      [回车 = 同一个号]：").strip().lower() in ("y", "yes"):
            while not wlan_account:
                wlan_account = input("      教室的账号：").strip()
            wlan_password = input("      教室的密码（直接回车 = 和宿舍一样）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n 已取消。")
        return False

    cfg = build_config(account, password, choice, wlan_account, wlan_password)
    try:
        save_config(cfg)
    except OSError as exc:
        print(" 写配置失败：%s" % exc)
        return False
    print("\n 已保存：%s" % CONFIG_PATH)
    print(" 共写了 %d 条账号写法（宿舍走运营商宽带、教室走校园网），"
          "程序会自己挑能用的那条。" % len(cfg["profiles"]))
    print(" 不确定运营商的话，用不上的那几条会自动跳过，不用管。")
    if wlan_account:
        print(" 教室用的是单独的号：%s" % wlan_account)
    print(" 接下来试一次认证，结果打印在下面。\n")
    return True


# ============================================================================
# 图形界面
# ============================================================================

def run_gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext
    except Exception as exc:
        print("打不开图形界面（没装 tkinter？）：%s" % exc)
        print("可以先用命令行：python3 %s status" % os.path.basename(__file__))
        return 1

    # Tk 8.5（macOS 系统自带 Python 里的那个）在新版 macOS 上画不出 Label / Entry / Frame，
    # 开出来是一大片空白。与其让用户对着空白窗口发懵，不如直接说清楚怎么换。
    if float(tk.TkVersion) < 8.6:
        print("这个 Python 的图形库太旧（Tk %.1f），窗口会显示为空白，已停止。" % tk.TkVersion)
        print("换一个带新 Tk 的 Python 来开界面：brew install python-tk@3.12")
        print("或者直接用命令行，功能完全一样：")
        print("  python3 %s init     配置账号" % os.path.basename(__file__))
        print("  python3 %s status   看状态" % os.path.basename(__file__))
        return 1

    cfg = load_config()
    log = Logger(cfg, verbose=False)

    COLORS = {
        "bg": "#1e1f22", "card": "#2b2d31", "text": "#e6e6e6", "dim": "#9aa0a6",
        "ok": "#3fb950", "warn": "#d29922", "bad": "#f85149", "accent": "#58a6ff",
    }

    root = tk.Tk()
    root.title("东南大学校园网自动认证")
    root.configure(bg=COLORS["bg"])
    root.geometry("560x620")
    root.minsize(480, 520)

    state = {"status": None, "busy": False}
    # worker 线程不直接碰界面：Tk 不是线程安全的，从子线程改控件或调 root.after
    # 可能抛 "main thread is not in main loop"，也可能把 Tcl 解释器搞坏。
    # 子线程只把「结果」放进队列，主线程用 after() 定时取出来渲染。
    ui_queue = queue.Queue()

    def ui_thread_safe(func):
        ui_queue.put(func)

    def pump_ui_queue():
        try:
            while True:
                func = ui_queue.get_nowait()
                try:
                    func()
                except Exception:
                    pass
        except queue.Empty:
            pass
        root.after(60, pump_ui_queue)

    title = tk.Label(root, text="校园网自动认证", bg=COLORS["bg"], fg=COLORS["text"],
                     font=("PingFang SC", 16, "bold"))
    title.pack(anchor="w", padx=18, pady=(16, 2))
    subtitle = tk.Label(root, text="macOS / Windows 通用 · 按网络环境自动挑选账号写法",
                        bg=COLORS["bg"], fg=COLORS["dim"], font=("PingFang SC", 11))
    subtitle.pack(anchor="w", padx=18, pady=(0, 10))

    card = tk.Frame(root, bg=COLORS["card"])
    card.pack(fill="x", padx=16, pady=6)

    status_label = tk.Label(card, text="正在检查…", bg=COLORS["card"], fg=COLORS["text"],
                            font=("PingFang SC", 15, "bold"))
    status_label.pack(anchor="w", padx=16, pady=(14, 2))
    detail_label = tk.Label(card, text="", bg=COLORS["card"], fg=COLORS["dim"],
                            font=("PingFang SC", 11), justify="left", anchor="w", wraplength=490)
    detail_label.pack(anchor="w", padx=16, pady=(0, 12))

    info_label = tk.Label(card, text="", bg=COLORS["card"], fg=COLORS["dim"],
                          font=("Menlo", 10), justify="left", anchor="w", wraplength=490)
    info_label.pack(anchor="w", padx=16, pady=(0, 14))

    buttons = tk.Frame(root, bg=COLORS["bg"])
    buttons.pack(fill="x", padx=16, pady=(6, 4))

    def open_path(path):
        try:
            if IS_MAC:
                subprocess.Popen(["open", path])
            elif IS_WIN:
                os.startfile(path)  # noqa: 只在 Windows 上存在
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("打不开", "%s\n%s" % (path, exc))

    def open_config():
        if not os.path.exists(CONFIG_PATH):
            try:
                save_config(cfg)
            except OSError as exc:
                messagebox.showerror("写配置失败", str(exc))
                return
        open_path(CONFIG_PATH)

    def open_log():
        open_path(log.path)

    def open_settings():
        """真正的设置对话框：直接在界面里填账号密码，不用去编辑 JSON。"""
        dialog = tk.Toplevel(root)
        dialog.title("设置账号密码")
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(root)
        dialog.resizable(False, False)

        current = load_config()

        def label(text, top=14):
            widget = tk.Label(dialog, text=text, bg=COLORS["bg"], fg=COLORS["text"],
                              font=("PingFang SC", 12), anchor="w")
            widget.pack(fill="x", padx=20, pady=(top, 2))
            return widget

        def hint(parent, text, wraplength=410):
            widget = tk.Label(parent, text=text, bg=COLORS["bg"], fg=COLORS["dim"],
                              font=("PingFang SC", 10), anchor="w", justify="left",
                              wraplength=wraplength)
            widget.pack(fill="x", padx=20, pady=(0, 4))
            return widget

        label("宿舍宽带（SEU-ISP）的账号", top=18)
        account_entry = tk.Entry(dialog, bg="#16171a", fg=COLORS["text"],
                                 insertbackground=COLORS["text"], relief="flat",
                                 font=("PingFang SC", 13))
        account_entry.pack(fill="x", padx=20, ipady=6)

        label("宿舍宽带的密码")
        password_entry = tk.Entry(dialog, bg="#16171a", fg=COLORS["text"],
                                  insertbackground=COLORS["text"], relief="flat",
                                  show="•", font=("PingFang SC", 13))
        password_entry.pack(fill="x", padx=20, ipady=6)

        # 教室（SEU-WLAN）的号可能和宿舍不一样，默认折叠起来，勾上才填。
        existing_wlan = {}
        for profile in (current.get("profiles") or []):
            if isinstance(profile, dict) and "SEU-WLAN" in (profile.get("ssids") or []):
                existing_wlan = profile
                break

        separate_var = tk.BooleanVar(
            value=bool(str(existing_wlan.get("account") or "").strip()))
        wlan_frame = tk.Frame(dialog, bg=COLORS["bg"])

        def fit_dialog():
            """按当前内容重新算尺寸并居中。

            展开「教室另一个号」后内容会变高，不重算的话对话框会被裁掉一截，
            底部的运营商选项和按钮就看不见了。
            """
            dialog.update_idletasks()
            width = max(dialog.winfo_reqwidth(), 420)
            height = dialog.winfo_reqheight()
            x = root.winfo_rootx() + (root.winfo_width() - width) // 2
            y = root.winfo_rooty() + max((root.winfo_height() - height) // 3, 20)
            dialog.geometry("%dx%d+%d+%d" % (width, height, max(x, 0), max(y, 0)))

        def toggle_wlan():
            if separate_var.get():
                wlan_frame.pack(fill="x", padx=0, before=carrier_block)
            else:
                wlan_frame.pack_forget()
            # 内容高度变了，重新排版并调整窗口
            dialog.update_idletasks()
            fit_dialog()

        tk.Checkbutton(
            dialog, text="教室的校园网（SEU-WLAN）用另一个号",
            variable=separate_var, command=toggle_wlan,
            bg=COLORS["bg"], fg=COLORS["text"], selectcolor="#3a3d42",
            activebackground=COLORS["bg"], activeforeground=COLORS["text"],
            font=("PingFang SC", 11), highlightthickness=0, bd=0, anchor="w"
        ).pack(fill="x", padx=18, pady=(10, 0))

        wlan_account_entry = tk.Entry(wlan_frame, bg="#16171a", fg=COLORS["text"],
                                      insertbackground=COLORS["text"], relief="flat",
                                      font=("PingFang SC", 13))
        wlan_account_entry.pack(fill="x", padx=20, ipady=6, pady=(4, 0))
        wlan_password_entry = tk.Entry(wlan_frame, bg="#16171a", fg=COLORS["text"],
                                       insertbackground=COLORS["text"], relief="flat",
                                       show="•", font=("PingFang SC", 13))
        wlan_password_entry.pack(fill="x", padx=20, ipady=6, pady=(4, 0))
        tk.Label(wlan_frame, text="教室的账号 / 密码（密码留空 = 和宿舍一样）",
                 bg=COLORS["bg"], fg=COLORS["dim"],
                 font=("PingFang SC", 10), anchor="w").pack(fill="x", padx=20, pady=(2, 0))

        carrier_block = tk.Frame(dialog, bg=COLORS["bg"])
        carrier_block.pack(fill="x")
        tk.Label(carrier_block, text="宿舍宽带的运营商", bg=COLORS["bg"], fg=COLORS["text"],
                 font=("PingFang SC", 12), anchor="w").pack(fill="x", padx=20, pady=(12, 2))
        hint(carrier_block, "不确定就直接用默认的「不确定」，程序会把三家都写上依次试。")

        # 已有的运营商选择：从 SEU-ISP 那几条的后缀反推。
        # 只有「恰好一条」时才能确定是哪家；如果有多条（说明当初选的是「不确定」，
        # 三家都配上了），必须回退成「不确定」，否则用户只是打开设置点个保存，
        # 就会因为预选成移动而把另外两条兜底配置静默删掉。
        isp_suffixes = [
            profile.get("account_suffix")
            for profile in (current.get("profiles") or [])
            if isinstance(profile, dict) and "SEU-ISP" in (profile.get("ssids") or [])
        ]
        current_carrier = ""
        if len(isp_suffixes) == 1:
            for key, (suffix, _name) in CARRIERS.items():
                if isp_suffixes[0] == suffix:
                    current_carrier = key
                    break

        carrier_var = tk.StringVar(value=current_carrier)
        choices = tk.Frame(carrier_block, bg=COLORS["bg"])
        choices.pack(fill="x", padx=20, pady=(2, 0))
        for value, text in (("", "不确定"), ("1", "移动"), ("2", "电信"), ("3", "联通")):
            tk.Radiobutton(
                choices, text=text, value=value, variable=carrier_var,
                bg=COLORS["bg"], fg=COLORS["text"], selectcolor="#3a3d42",
                activebackground=COLORS["bg"], activeforeground=COLORS["text"],
                font=("PingFang SC", 12), highlightthickness=0, bd=0
            ).pack(side="left", padx=(0, 14))

        # 预填已有值
        account_entry.insert(0, str(current.get("account") or ""))
        password_entry.insert(0, str(current.get("password") or ""))
        wlan_account_entry.insert(0, str(existing_wlan.get("account") or ""))
        wlan_password_entry.insert(0, str(existing_wlan.get("password") or ""))
        if separate_var.get():
            toggle_wlan()

        message = tk.Label(dialog, text="", bg=COLORS["bg"], fg=COLORS["bad"],
                           font=("PingFang SC", 11), anchor="w", wraplength=410)
        message.pack(fill="x", padx=20, pady=(10, 0))

        def do_save():
            account = account_entry.get().strip()
            password = password_entry.get().strip()
            if not account:
                message.configure(text="宿舍宽带的账号不能为空。")
                account_entry.focus_set()
                return
            if not password:
                message.configure(text="宿舍宽带的密码不能为空。")
                password_entry.focus_set()
                return
            wlan_account = wlan_password = ""
            if separate_var.get():
                wlan_account = wlan_account_entry.get().strip()
                if not wlan_account:
                    message.configure(text="勾了「教室用另一个号」，就要填教室的账号。")
                    wlan_account_entry.focus_set()
                    return
                wlan_password = wlan_password_entry.get().strip()
            try:
                save_config(build_config(account, password, carrier_var.get(),
                                         wlan_account, wlan_password))
            except OSError as exc:
                message.configure(text="保存失败：%s" % exc)
                return
            dialog.destroy()
            status_label.configure(text="设置已保存，正在认证…", fg=COLORS["accent"])
            refresh()
            run_action("login")

        def on_return(_event):
            do_save()

        for entry in (account_entry, password_entry, wlan_account_entry, wlan_password_entry):
            entry.bind("<Return>", on_return)

        row = tk.Frame(dialog, bg=COLORS["bg"])
        row.pack(fill="x", padx=20, pady=(14, 0))
        save_btn = make_button(row, "保存并认证", do_save)
        save_btn.pack(side="right")
        cancel_btn = make_button(row, "取消", dialog.destroy)
        cancel_btn.pack(side="right", padx=(0, 10))

        dialog.grab_set()
        # 按内容自适应尺寸并居中（不写死 460x400，否则内容放不下时会露出来挡住主界面）
        fit_dialog()
        account_entry.focus_set()
        return dialog

    def make_button(parent, text, command):
        """用 Label 模拟按钮。

        macOS 的 aqua 主题下 tk.Button 会忽略 bg/fg（画成系统样式的浅灰底 + 浅灰字，
        在这个深色界面上几乎看不见），所以自己画一个，颜色完全可控。
        """
        label = tk.Label(parent, text=text, bg="#3a3d42", fg=COLORS["text"],
                         font=("PingFang SC", 12), padx=14, pady=7, cursor="hand2")
        label._enabled = True

        def on_click(_event):
            if label._enabled:
                command()

        def on_enter(_event):
            if label._enabled:
                label.configure(bg="#4a4e55")

        def on_leave(_event):
            label.configure(bg="#3a3d42" if label._enabled else "#2b2d31")

        label.bind("<Button-1>", on_click)
        label.bind("<Enter>", on_enter)
        label.bind("<Leave>", on_leave)

        def set_enabled(enabled):
            label._enabled = enabled
            label.configure(bg="#3a3d42" if enabled else "#2b2d31",
                            fg=COLORS["text"] if enabled else "#5a5f66",
                            cursor="hand2" if enabled else "arrow")

        label.set_enabled = set_enabled
        return label

    # 主要操作用一行，次要的放第二行，避免 6 个按钮在窄窗口里挤成一团
    primary = tk.Frame(root, bg=COLORS["bg"])
    primary.pack(fill="x", padx=16, pady=(6, 2))
    secondary = tk.Frame(root, bg=COLORS["bg"])
    secondary.pack(fill="x", padx=16, pady=(0, 4))

    button_login = make_button(primary, "立即认证", lambda: run_action("login"))
    button_login.pack(side="left", padx=(0, 8))
    button_logout = make_button(primary, "注销下线", lambda: run_action("logout"))
    button_logout.pack(side="left", padx=(0, 8))
    button_refresh = make_button(primary, "刷新状态", lambda: refresh())
    button_refresh.pack(side="left")

    button_settings = make_button(secondary, "设置账号", open_settings)
    button_settings.pack(side="left", padx=(0, 8))
    button_config = make_button(secondary, "配置原文", open_config)
    button_config.pack(side="left", padx=(0, 8))
    button_log = make_button(secondary, "打开日志", open_log)
    button_log.pack(side="left")

    tail_title = tk.Label(root, text="最近日志", bg=COLORS["bg"], fg=COLORS["dim"],
                          font=("PingFang SC", 11))
    tail_title.pack(anchor="w", padx=18, pady=(10, 2))
    tail = scrolledtext.ScrolledText(root, height=12, bg="#16171a", fg="#c9d1d9",
                                     insertbackground="#c9d1d9", relief="flat",
                                     font=("Menlo", 10), wrap="word")
    tail.pack(fill="both", expand=True, padx=16, pady=(0, 14))

    def apply_status(info):
        state["status"] = info
        if info["connectivity"]:
            status_label.configure(text="已联网", fg=COLORS["ok"])
            detail_label.configure(text="外网可用，不需要额外操作。")
        elif info["online"]:
            status_label.configure(text="门户显示在线，但外网不通", fg=COLORS["warn"])
            detail_label.configure(text="可能会话卡住了，点「立即认证」会先注销再重试。")
        elif info["portal"]:
            status_label.configure(text="已连校园网，未认证", fg=COLORS["bad"])
            detail_label.configure(text="点「立即认证」即可上线。")
        else:
            status_label.configure(text="不在校园网 / 没有网络", fg=COLORS["dim"])
            detail_label.configure(text="%s\n%s" % (info["online_detail"][:160], wifi_offline_hint()))

        lines = [
            "网络   %s" % (info["ssid"] if info["ssid_readable"] else "读不到（系统未授权定位，不影响认证）"),
            "本机   %s    网关 %s" % (info["ip"] or "-", info["gateway"] or "-"),
            "门户   %s" % (info["portal"] or "未发现"),
            "外网   %s" % info["connectivity_detail"][:70],
        ]
        if info["uid"]:
            lines.append("账号   %s" % info["uid"])
        auto = autostart_status()
        lines.append("自启   %s" % ("开" if auto["running"] else "关")
                     + "（%s）" % auto["detail"][:50])
        info_label.configure(text="\n".join(lines))

        lines_tail = log.tail(200)
        tail.configure(state="normal")
        tail.delete("1.0", "end")
        tail.insert("end", "\n".join(lines_tail) if lines_tail else "（还没有日志）")
        tail.see("end")
        tail.configure(state="disabled")

    def set_busy(busy, text=""):
        state["busy"] = busy
        for button in (button_login, button_logout, button_refresh,
                       button_settings, button_config, button_log):
            button.set_enabled(not busy)
        if text:
            status_label.configure(text=text, fg=COLORS["accent"])

    def refresh():
        if state["busy"]:
            return
        set_busy(True, "检查中…")

        def work():
            try:
                info = collect_status(load_config())
            except Exception as exc:
                info = {"error": str(exc)}
            ui_thread_safe(lambda: finish_refresh(info))

        def finish_refresh(info):
            set_busy(False)
            if "error" in info:
                status_label.configure(text="检查失败", fg=COLORS["bad"])
                detail_label.configure(text=info["error"])
            else:
                apply_status(info)

        threading.Thread(target=work, daemon=True).start()

    def run_action(action):
        if state["busy"]:
            return
        set_busy(True, "认证中…" if action == "login" else "注销中…")

        def work():
            # 后台工作整段兜住异常：否则线程一死，finish_action 永远不会入队，
            # 界面就卡在「认证中…」并且所有按钮都点不动。
            code = 1
            message = ""
            try:
                cfg_now = load_config()
                log_now = Logger(cfg_now, verbose=False)
                if action == "login":
                    code = authenticate(cfg_now, log_now, once=False)
                else:
                    portal, detail = find_portal(
                        make_opener(), cfg_now,
                        float(cfg_now.get("request_timeout_seconds", 4)))
                    if portal:
                        ok, _text = do_logout(portal, cfg_now, log_now)
                        # 注销失败要让用户看到红色「未成功」，不能一律显示完成
                        code = 0 if ok else 1
                        message = "" if ok else "门户拒绝注销"
                    else:
                        log_now("找不到门户，无法注销")
                        code = 1
                        message = "找不到门户"
            except Exception as exc:
                code = 1
                message = "%s: %s" % (exc.__class__.__name__, exc)

            def finish_action():
                set_busy(False)
                ok = code == 0
                status_label.configure(
                    text="完成" if ok else ("未成功" + (("（%s）" % message) if message else "")),
                    fg=COLORS["ok"] if ok else COLORS["bad"])
                refresh()

            ui_thread_safe(finish_action)

        threading.Thread(target=work, daemon=True).start()

    refresh()
    pump_ui_queue()

    if not has_credentials(cfg):
        # 第一次用（或配置不全）：直接把设置对话框弹出来，别让用户自己去改 JSON。
        status_label.configure(text="还没有填账号密码", fg=COLORS["warn"])
        detail_label.configure(text="填好一卡通号和密码就能用了，填完会自动认证一次。")
        root.after(300, open_settings)

    root.mainloop()
    return 0


# ============================================================================
# 开机自启：launchd（macOS）/ 任务计划（Windows）
# ============================================================================

LAUNCHD_LABEL = "cn.edu.seu.net.autologin"
WINDOWS_TASK = "SEU-Net-AutoLogin"


def _launchd_plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL)


def autostart_status():
    """开机自启现在是装了还是没装、跑得怎么样。给 status / doctor / GUI 用。

    返回 dict：installed / running / detail / last_run
    """
    info = {"installed": False, "running": False, "detail": "", "last_run": ""}
    if IS_MAC:
        path = _launchd_plist_path()
        if not os.path.exists(path):
            info["detail"] = "没装（跑 `seu_net.py install` 装上）"
            return info
        info["installed"] = True
        code, out, err = run_result(["launchctl", "print", "gui/%d/%s"
                                     % (os.getuid(), LAUNCHD_LABEL)])
        if code != 0:
            info["detail"] = ("配置在 %s，但 launchd 没加载它"
                              "（跑一次 install 重新装载）" % path)
            return info
        info["running"] = True
        interval = ""
        # launchctl print 的格式是 `run interval = 15 seconds`
        match = re.search(r"run interval\s*=\s*(\d+)\s*second", out)
        if match:
            interval = "每 %s 秒检查一次" % match.group(1)
        info["detail"] = "已装载" + (("，" + interval) if interval else "")
        exit_code = re.search(r"last exit code\s*=\s*(-?\d+)", out)
        if exit_code:
            info["detail"] += "，上次退出码 %s" % exit_code.group(1)
        return info

    code, out, _err = run_result(["schtasks", "/Query", "/TN", WINDOWS_TASK],
                                 encoding="gbk")
    if code != 0:
        info["detail"] = "没装（跑 `seu_net.py install` 装上）"
        return info
    info["installed"] = True
    info["running"] = "Ready" in out or "就绪" in out or "正在运行" in out
    info["detail"] = "计划任务已注册" + ("" if info["running"] else "（当前不是就绪状态）")
    return info


def _pick_python():
    """挑一个稳定的 Python 解释器写进自启配置。

    不能用 sys.executable 直接写死：可能是个带版本号的临时路径（比如工具链目录），
    升级后路径就没了，自启会静默失效。优先用系统自带的。
    """
    candidates = ["/usr/bin/python3"] if IS_MAC else []
    if getattr(sys, "frozen", False):
        return sys.executable
    candidates.append(sys.executable)
    for path in candidates:
        if path and os.path.exists(path):
            out = run([path, "-c", "import sys;print(sys.version_info[0])"])
            if out.strip() == "3":
                return path
    return sys.executable or "python3"


def _autostart_command():
    """拼出「怎么启动本程序」的参数表。

    打包成独立程序（PyInstaller 等）时 sys.executable 就是程序本身、不需要脚本路径；
    源码运行时则是 `python3 脚本 子命令`。两种情况都要能得到一个稳定的绝对路径，
    否则写进自启配置后重启就失效。
    """
    if getattr(sys, "frozen", False):
        return [os.path.abspath(sys.executable)]
    return [os.path.abspath(_pick_python()), os.path.abspath(__file__)]


# 这几个文件会在网络切换（换 WiFi、插网线、DHCP 续约）时被系统重写。
# 用 WatchPaths 盯住它们，换网络那一刻就能立刻触发认证，
# 不用等 StartInterval 那个周期——宿舍到教室这种场景差别很明显。
#
# 注意：WatchPaths 只在文件被**替换（rename）**时触发，原地改写内容不会触发。
# 实测确认过：`mv` 覆盖会触发，`>>` 追加和 `touch` 不会。
# configd 更新 resolv.conf 用的是写临时文件再 rename，正好符合。
WATCH_PATHS = [
    "/var/run/resolv.conf",
    "/etc/resolv.conf",
]


def install_autostart(interval=60, log=print, tray=False):
    """装开机自启。

    tray=True 时装的是菜单栏托盘（需要 pyobjc，且不设 StartInterval——
    托盘自己常驻、自己定时刷新）。默认装的是轻量的 `login --quiet` 定时任务，
    只用标准库，不会因为某个第三方依赖没装而静默失效。
    """
    launcher = _autostart_command()
    if IS_MAC:
        if tray:
            # 托盘需要 pyobjc，而 pyobjc 未必装在系统自带的 Python 上，
            # 所以这里必须用「当前正在运行、已经能 import AppKit 的那个解释器」，
            # 不能用 _pick_python()（它会优先挑 /usr/bin/python3，那里没有 pyobjc）。
            code, _out, _err = run_result(
                [sys.executable, "-c", "import AppKit"])
            if code != 0:
                log("装托盘需要 pyobjc，但当前解释器（%s）里没有。" % sys.executable)
                log("先装上：%s -m pip install pyobjc-framework-Cocoa" % sys.executable)
                log("装好后再跑一次 `python3 seu_tray.py --install`。")
                return 1
            tray_script = os.path.join(BASE_DIR, "seu_tray.py")
            args = [os.path.abspath(sys.executable), tray_script]
            extra = ""
        else:
            args = launcher + ["login", "--quiet"]
            extra = "  <key>StartInterval</key><integer>%d</integer>\n" % int(interval)
        plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
{extra}  <!-- 换 WiFi / 插网线 / DHCP 变化时这几个文件会被重写，盯住它们就能在网络
       切换的瞬间触发认证，不用干等周期。 -->
  <key>WatchPaths</key>
  <array>
{watch}
  </array>
  <!-- ThrottleInterval 防止网络抖动时连环触发：两次启动至少隔 10 秒 -->
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- 程序自己会写 login.log（带轮转）。这里的 stdout/stderr 只装崩溃堆栈这类
       意外输出，指向 /dev/null 避免一年下来堆成一个几百 MB 的文件；
       真出问题看 login.log 和 doctor 就够了。 -->
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
""".format(
            label=LAUNCHD_LABEL,
            args="\n".join("    <string>%s</string>" % part for part in args),
            extra=extra,
            watch="\n".join("    <string>%s</string>" % p for p in WATCH_PATHS))
        path = _launchd_plist_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(plist)
        uid = os.getuid()
        run(["launchctl", "bootout", "gui/%d/%s" % (uid, LAUNCHD_LABEL)])
        code, _out, err = run_result(["launchctl", "bootstrap", "gui/%d" % uid, path])
        if code != 0:
            log("写入 %s 成功，但 launchctl bootstrap 失败（退出码 %d）：%s"
                % (path, code, (err or _out).strip()[:200]))
            log("常见原因：这个 Label 已被别的 launchd 会话占用，先跑 uninstall 再装一次。")
            return 1
        run(["launchctl", "enable", "gui/%d/%s" % (uid, LAUNCHD_LABEL)])
        log("已安装开机自启：%s" % path)
        if tray:
            log("开机后常驻菜单栏，点图标可以用；状态每 30 秒自动刷新。")
        else:
            log("每 %d 秒检查一次；日志在 %s"
                % (int(interval), os.path.join(BASE_DIR, "login.log")))
        log("要停用跑 `%s uninstall`" % os.path.basename(launcher[-1]))
        return 0

    # Windows：登录后触发 + 每 N 分钟。
    # --interval 的语义统一为「秒」，但 schtasks 最小粒度是 1 分钟，
    # 所以这里向上折算成分钟，并在提示里写清楚实际是多少。
    task = "SEU-Net-AutoLogin"
    minutes = max(1, int(round(int(interval) / 60.0)))
    run(["schtasks", "/Delete", "/TN", task, "/F"])
    cmd = "%s login --quiet" % " ".join('"%s"' % part for part in launcher)
    code, out, err = run_result(
        ["schtasks", "/Create", "/TN", task, "/TR", cmd, "/SC", "MINUTE",
         "/MO", str(minutes), "/F", "/RL", "LIMITED"], encoding="gbk")
    if code != 0:
        log("创建计划任务 %s 失败（退出码 %d）：%s" % (task, code, (err or out).strip()[:200]))
        return 1
    log("已创建计划任务 %s（每 %d 分钟检查一次）" % (task, minutes))
    if int(interval) < 60:
        log("  注意：Windows 计划任务最小间隔是 1 分钟，所以实际按每 1 分钟执行。")
    return 0


def uninstall_autostart(log=print):
    if IS_MAC:
        path = _launchd_plist_path()
        uid = os.getuid()
        run(["launchctl", "bootout", "gui/%d/%s" % (uid, LAUNCHD_LABEL)])
        if os.path.exists(path):
            os.remove(path)
            log("已移除 %s" % path)
        else:
            log("没有找到已安装的自启配置")
        return 0
    run(["schtasks", "/Delete", "/TN", "SEU-Net-AutoLogin", "/F"])
    log("已删除计划任务 SEU-Net-AutoLogin")
    return 0


# ============================================================================
# 自检
# ============================================================================

def doctor(cfg, log):
    log("===== 环境自检 =====")
    log("平台：%s" % platform.platform())
    log("Python：%s（%s）" % (platform.python_version(), sys.executable))
    # Tk 8.5 在新版 macOS 上画不出 Label / Entry / Frame（只有 Button 和 Radiobutton 能显示），
    # 界面会是一片空白。这里主动报出来，省得用户以为是程序坏了。
    try:
        import tkinter
        tk_version = float(tkinter.TkVersion)
        if tk_version >= 8.6:
            log("图形界面：Tk %.1f，正常" % tk_version)
        else:
            log("图形界面：Tk %.1f —— 版本太旧，窗口会显示为空白。" % tk_version)
            log("  处理：换一个带新 Tk 的 Python 来开界面（brew install python-tk），"
                "或直接用命令行。")
    except Exception:
        log("图形界面：没装 tkinter，只能用命令行")
    log("程序目录：%s" % BASE_DIR)
    log("配置文件：%s（%s）" % (CONFIG_PATH, "存在" if os.path.exists(CONFIG_PATH) else "不存在"))
    if cfg.get("_config_error"):
        log("配置问题：%s" % cfg["_config_error"])
    log("账号已填：%s" % ("是" if has_credentials(cfg) else "否（缺一卡通号或密码）"))

    ssid = current_ssid(cfg)
    log("当前 SSID：%s" % (ssid or "读不到"))
    if IS_MAC and not ssid:
        log("  说明：macOS 从 Sonoma 起，没授予「定位服务」权限的程序读不到 SSID（只显示 <redacted>）。")
        log("  影响：不影响认证。程序会改用网关识别网络，并逐条尝试 profiles 里的账号写法。")
    log("本机 IP：%s    默认网关：%s" % (primary_ip() or "-", default_gateway() or "-"))
    log("网络标识：%s" % network_fingerprint(cfg, ssid))

    portal, info = find_portal(make_opener(), cfg, float(cfg.get("request_timeout_seconds", 4)))
    log("门户：%s（%s）" % (portal["base"] if portal else "未发现", info))
    if portal:
        online, detail, uid = query_online(portal, cfg, float(cfg.get("request_timeout_seconds", 4)))
        log("门户状态：%s%s" % (detail, ("（账号 %s）" % uid) if uid else ""))
    ok, detail = connectivity_ok(cfg, float(cfg.get("request_timeout_seconds", 4)))
    log("外网：%s（%s）" % ("通" if ok else "不通", detail))

    auto = autostart_status()
    log("开机自启：%s" % auto["detail"])
    holder = lock_holder()
    log("当前认证进程：%s" % (holder or "无"))
    total = Logger(cfg, verbose=False).total_bytes()
    log("日志占用：%.1f KB（上限 %s KB × %s 份）"
        % (total / 1024.0, cfg.get("log_max_kb"), int(cfg.get("log_backup_count", 3)) + 1))
    log("自检结束。")
    return 0


# ============================================================================
# 命令行入口
# ============================================================================

USAGE = """东南大学校园网自动认证 v{VERSION}

用法：python3 seu_net.py <子命令>

  login [--once] [--quiet]  认证（默认一直重试到成功或超时；--once 只试一轮）
  status                    看当前状态（只读，不改网络）
  logout                    让本机下线
  gui                       打开小窗口
  init                      首次配置向导
  install [--interval 秒]   装成开机自启（默认每 60 秒检查一次）
  uninstall                 卸载开机自启
  doctor                    环境自检（出问题先跑这个）
  version                   版本号
""".format(VERSION=VERSION)


def do_logout(portal, cfg, log=None):
    """提交注销并如实报告结果。返回 (成功与否, 说明)。

    门户成功时返回不含 'failed' 的提示；失败是 `Logout failed.`。
    这里必须看返回内容，不能只要请求没抛异常就报成功。
    """
    say = log or (lambda message: None)
    try:
        _status, raw = http_request(make_opener(), build_logout_url(portal, cfg), timeout=10)
    except Exception as exc:
        say("注销请求失败：%s: %s" % (exc.__class__.__name__, exc))
        return False, str(exc)
    text = re.sub(r"\s+", " ", decode_body(raw)).strip()
    if "failed" in text.lower() or "fail" in text.lower():
        say("门户拒绝注销（返回：%s）。通常因为本机已不在线，或该 AC 不支持 Portal 注销" % text[:80])
        return False, text
    say("注销成功：%s" % (text[:80] or "门户未返回内容"))
    return True, text


def _scenes_command(cfg):
    """输出场景列表 + 当前场景，给界面渲染切换按钮。"""
    print(json.dumps({
        "scenes": scenes(cfg),
        "active": active_scene(cfg),
        "manual": load_state().get("manual_scene") or "",
    }, ensure_ascii=False, indent=2))
    return 0


def _switch_command(cfg, key, connect_wifi):
    """切换场景并认证，结果以 JSON 输出。"""
    log = Logger(cfg)
    result = switch_scene(cfg, log, key, connect_wifi=connect_wifi)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _carrier_choice(cfg):
    """从宿舍场景的后缀反推运营商选择（"1"/"2"/"3"，不确定返回空）。

    只有恰好一条 SEU-ISP 配置时才反推；多条说明当初选的是「不确定」。
    """
    suffixes = [
        p.get("account_suffix")
        for p in (cfg.get("profiles") or [])
        if isinstance(p, dict) and "SEU-ISP" in (p.get("ssids") or [])
    ]
    if len(suffixes) != 1:
        return ""
    for key, (suffix, _name) in CARRIERS.items():
        if suffixes[0] == suffix:
            return key
    return ""


def _config_command(cfg, flags):
    """输出界面能用的配置视图（不泄露密码明文）。"""
    stations = []
    for index, profile in enumerate(cfg.get("profiles") or []):
        if not isinstance(profile, dict):
            continue
        password = str(profile.get("password") or "")
        stations.append({
            "name": _profile_label(profile, index),
            "ssids": list(profile.get("ssids") or []),
            "account": str(profile.get("account") or cfg.get("account") or ""),
            "has_password": bool(password or cfg.get("password")),
            "account_suffix": str(profile.get("account_suffix") or ""),
            "priority": profile.get("priority", 100),
        })
    view = {
        "account": str(cfg.get("account") or ""),
        "has_password": bool(str(cfg.get("password") or "").strip()),
        "carrier": _carrier_choice(cfg),
        "stations": stations,
        "scenes": scenes(cfg),
        "config_path": CONFIG_PATH,
        "data_dir": BASE_DIR,
        "config_error": cfg.get("_config_error", ""),
    }
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


def _existing_wlan_password(cfg):
    """从现有配置里取出 SEU-WLAN 那条自己单独设的密码（没有就返回空）。"""
    for profile in cfg.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        names = [str(n).upper() for n in (profile.get("ssids") or [])]
        if any("SEU-WLAN" in name for name in names):
            return str(profile.get("password") or "")
    return ""


def _save_command():
    """从 stdin 读一份 {account, password, carrier, wlan_account, wlan_password} 写配置。

    密码留空表示「沿用旧密码」——界面上密码框是掩码，用户不改就不该把它清空。
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": "参数不是合法 JSON：%s" % exc},
                         ensure_ascii=False))
        return 1
    # null / [] / "abc" 都是合法 JSON，但后面 .get() 会抛 AttributeError，
    # 前端就只能看到一坨 traceback
    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "error": "参数必须是一个 JSON 对象"},
                         ensure_ascii=False))
        return 1

    old = load_config()
    account = str(payload.get("account") or "").strip()
    password = str(payload.get("password") or "")
    wlan_account = str(payload.get("wlan_account") or "").strip()
    wlan_password = str(payload.get("wlan_password") or "")
    carrier = str(payload.get("carrier") or "")

    if not account:
        print(json.dumps({"ok": False, "error": "账号不能为空"}, ensure_ascii=False))
        return 1
    if not password:
        # 留空 = 沿用原来的密码
        password = str(old.get("password") or "")
    if not password:
        print(json.dumps({"ok": False, "error": "密码不能为空"}, ensure_ascii=False))
        return 1

    # 教室密码留空同样表示「沿用旧密码」。
    # 不处理的话，build_config 不会给 SEU-WLAN 那条写 password，
    # 登录时会回退到宿舍密码——用户只是改个运营商，教室的密码就被顶掉了。
    if wlan_account and not wlan_password:
        wlan_password = _existing_wlan_password(old)

    cfg = build_config(account, password, carrier, wlan_account, wlan_password)
    try:
        save_config(cfg)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "写入失败：%s" % exc},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "config_path": CONFIG_PATH}, ensure_ascii=False))
    return 0


def main(argv):
    flags = [arg for arg in argv if arg.startswith("--")]
    args = [arg for arg in argv if not arg.startswith("-")]

    # --help / -h 是全局开关，不能被当成「没有子命令 → 默认 login」，
    # 否则 `seu_net.py --help` 会真的去认证一次。
    if "--help" in flags or "-h" in argv:
        print(USAGE)
        return 0
    if "--version" in flags:
        print("东南大学校园网自动认证 v%s" % VERSION)
        return 0

    command = args[0] if args else "login"

    if command in ("help", "version"):
        print(USAGE)
        return 0

    cfg = load_config()

    if command == "config":
        # 给界面用的：读当前配置，密码只给长度不给明文。
        # 前端只需要知道「填没填」，回填时用掩码占位，用户不改就不会覆盖。
        return _config_command(cfg, flags)

    if command == "scenes":
        # 界面主区按这个渲染场景切换按钮
        return _scenes_command(cfg)

    if command == "switch":
        # switch <场景key> [--no-wifi]
        keys = [a for a in args[1:] if not a.startswith("-")]
        if not keys:
            print(json.dumps({"ok": False, "message": "缺少场景名"},
                             ensure_ascii=False))
            return 1
        return _switch_command(cfg, keys[0], "--no-wifi" not in flags)

    if command == "save":
        # 从 stdin 读 JSON 配置写盘（界面保存用）。密码传空串表示「保持原样」。
        return _save_command()

    if command == "status":
        info = collect_status(cfg)
        auto = autostart_status()
        info["autostart"] = auto
        if "--json" in flags:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        print("平台：%s" % info["platform"])
        print("网络：%s" % (info["ssid"] if info["ssid_readable"] else "读不到 SSID（未授权定位，不影响认证）"))
        print("本机：%s    网关：%s" % (info["ip"] or "-", info["gateway"] or "-"))
        print("门户：%s" % (info["portal"] or "未发现（%s）" % info["online_detail"][:60]))
        print("门户状态：%s" % info["online_detail"])
        if info["uid"]:
            print("账号：%s" % info["uid"])
        print("外网：%s（%s）" % ("通" if info["connectivity"] else "不通",
                                info["connectivity_detail"][:90]))
        print("自启：%s" % auto["detail"])
        holder = lock_holder()
        if holder:
            print("当前有一个认证进程在跑：%s" % holder)
        return 0

    if command == "logout":
        portal, detail = find_portal(make_opener(), cfg, float(cfg.get("request_timeout_seconds", 4)))
        if not portal:
            print("找不到门户：%s" % detail)
            return 1
        ok, _text = do_logout(portal, cfg, log=lambda message: print(message))
        return 0 if ok else 1

    if command == "gui":
        return run_gui()

    if command == "tray":
        # 托盘在单独一个进程里跑：tkinter 和 AppKit 都要占主线程的消息循环，
        # 塞进同一个进程会互相卡死。
        try:
            import seu_tray
        except ImportError as exc:
            print("托盘功能不可用：%s" % exc)
            print("命令行和图形界面照常可用。")
            return 1
        return seu_tray.main(flags)

    if command == "init":
        if not first_run_wizard(force=True):
            if not (sys.stdin and sys.stdin.isatty()):
                print("init 需要在终端里交互输入。没有终端时，请直接编辑 config.json"
                      "（照着 config.example.json 填 account / password）。")
            return 1
        cfg = load_config()
        # 配好之后顺手试一次，让用户当场看到能不能通
        return authenticate(cfg, Logger(cfg))

    if command == "install":
        interval = 60
        for index, arg in enumerate(argv):
            if arg == "--interval" and index + 1 < len(argv):
                try:
                    interval = max(15, int(argv[index + 1]))
                except ValueError:
                    pass
        return install_autostart(interval)

    if command == "uninstall":
        return uninstall_autostart()

    if command == "doctor":
        return doctor(cfg, Logger(cfg))

    if command != "login":
        print(USAGE)
        return 64

    # login
    quiet = "--quiet" in flags
    if not has_credentials(cfg):
        if not quiet and first_run_wizard():
            cfg = load_config()
        if not has_credentials(cfg):
            print("配置不完整：缺少一卡通号或密码。先运行 `seu_net.py init`。")
            return 3
    log = Logger(cfg, verbose=not quiet)
    try:
        return authenticate(cfg, log, once="--once" in flags)
    except KeyboardInterrupt:
        return 130


def start_tray_sidecar():
    """在后台拉起菜单栏托盘（独立进程）。

    tkinter 和 AppKit 都要独占主线程的消息循环，放同一个进程会互相卡死，
    所以托盘另起一个进程。用文件锁保证只会有一个托盘在跑——
    双击两次 app 不会出现两个图标。
    """
    tray_pid_file = os.path.join(BASE_DIR, "tray.pid")
    # 已经在跑就别重复起
    try:
        with open(tray_pid_file, "r") as fh:
            old_pid = int(fh.read().strip() or 0)
        if old_pid > 0:
            os.kill(old_pid, 0)          # 只探测存活，不发信号
            return True
    except (OSError, ValueError):
        pass

    try:
        import seu_tray  # noqa: F401  只是为了确认这个环境能用托盘
    except Exception:
        return False

    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "seu_tray.py")]
            if not getattr(sys, "frozen", False)
            else [sys.executable, "tray"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,     # 和当前进程脱钩，关掉窗口托盘还在
        )
    except Exception:
        return False
    try:
        with open(tray_pid_file, "w") as fh:
            fh.write(str(proc.pid))
    except OSError:
        pass
    return True


if __name__ == "__main__":
    argv = sys.argv[1:]
    # 打包成 .app 双击时没有任何命令行参数，也没有终端——这种时候开界面，
    # 并且顺手把菜单栏托盘也拉起来（单独进程，各占各的消息循环）。
    if not argv and getattr(sys, "frozen", False) and (sys.stdin is None or not sys.stdin.isatty()):
        start_tray_sidecar()
        argv = ["gui"]
    sys.exit(main(argv))
