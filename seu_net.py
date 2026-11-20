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

# 打包成 exe / app 后 __file__ 指向临时解包目录，配置和日志要跟着程序本身走
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

# GUI 子进程启动控制台程序时不要弹窗口（Windows）
NO_WINDOW = 0x08000000 if IS_WIN else 0


# ============================================================================
# 默认配置
# ============================================================================

DEFAULT_CONFIG = {
    "account": "",
    "password": "",

    # 按网络环境排的账号写法。实际提交的账号 = account_prefix + account + account_suffix
    # 程序会挑「这个环境能用」的那份，所以多写几条也没关系，写错的那条会被自动跳过。
    "profiles": [
        {
            "name": "运营商宽带（宿舍，优先）",
            "ssids": ["SEU-ISP"],
            "account_prefix": "",
            "account_suffix": "@cmcc",
            "priority": 10,
            "note": "移动 @cmcc / 电信 @dx / 联通 @lt",
        },
        {
            "name": "校园网（教室）",
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
    os.replace(tmp, CONFIG_PATH)


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


def join_wifi(ssid, cfg):
    """主动连指定无线网络（校园网都是开放网络，不需要密码）。"""
    if not ssid:
        return False
    if IS_MAC:
        device = cfg.get("wifi_interface") or wifi_device()
        out = run(["networksetup", "-setairportnetwork", device, ssid], timeout=25)
        ok = ("could not find" not in out.lower()) and ("error" not in out.lower())
        return ok
    device = cfg.get("wifi_interface") or ""
    cmd = ["netsh", "wlan", "connect", "name=%s" % ssid]
    if device:
        cmd.append("interface=%s" % device)
    out = run(cmd, timeout=25, encoding="gbk")
    return "completed" in out.lower() or "成功" in out


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


def classify_login(text):
    """把门户的登录返回分成 ok / online / authfail / unknown。

    成功时门户返回含 `Msg=0`，或回一段引用了 Dr.COMWebLoginID_3.htm 的成功页；
    失败时是 `Msg=01;msga='用户名或密码错误！'; Login fail.`
    """
    body = text or ""
    lowered = body.lower()
    if any(key in lowered for key in AUTH_FAIL_KEYS):
        detail = _portal_message(body) or "认证被拒绝"
        return "authfail", detail
    if "msga=" in lowered or "msg=" in lowered:
        code = re.search(r"msg\s*=\s*'?(\d+)", lowered)
        if code and code.group(1) != "0":
            return "authfail", _portal_message(body) or ("Msg=%s" % code.group(1))
    if "webloginid_3" in lowered or re.search(r"msg\s*=\s*'?0\b", lowered):
        return "ok", _portal_message(body) or "门户返回成功"
    if "webloginid_2" in lowered:
        return "authfail", _portal_message(body) or "门户返回失败页"
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


def forget_profile(fingerprint):
    state = load_state()
    (state.get("profiles") or {}).pop(fingerprint, None)
    save_state(state)


# ============================================================================
# 认证主流程
# ============================================================================

def try_login_once(cfg, log, portal, profile, index, timeout, login_timeout):
    """用一份配置提交一次认证，并以外网是否真的通为最终判据。"""
    creds = _profile_credentials(profile, cfg)
    if not creds["password"]:
        log("    %s：没有密码，跳过" % _profile_label(profile, index))
        return "skip", ""
    label = _profile_label(profile, index)
    url = build_login_url(portal, cfg, creds)
    try:
        _status, raw = http_request(make_opener(), url, timeout=login_timeout)
        text = decode_body(raw)
        state, detail = classify_login(text)
    except Exception as exc:
        state, detail = "unknown", "%s: %s" % (exc.__class__.__name__, exc)

    log("  提交认证（%s｜账号 %s）→ %s" % (label, creds["login_name"], detail))
    if state == "authfail":
        return "authfail", detail

    if wait_connectivity(cfg, log, timeout):
        return "ok", detail
    return "neterr", detail


def authenticate(cfg, log, once=False, max_wait=None):
    """完整的认证流程，返回退出码：0 成功 / 1 超时 / 2 账号被拒 / 3 配置不全。"""
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

    # 已经能上网就什么都不做。定时任务每 60 秒跑一次，不先短路的话
    # 每次都会重新提交认证、把日志刷满，还可能把刚稳定的会话踢掉。
    reachable, _detail = connectivity_ok(cfg, timeout)
    if reachable:
        log("外网已通，不需要认证（%s，%s）" % (reason, fingerprint))
        return 0

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
                if position < len(ordered):
                    log("    这一份没成，换下一份配置")

            # 没有任何一条配置真正提交过（全被跳过）说明配置有问题；
            # 提交过的全被门户拒绝说明账号密码有问题。两种都不该继续重试。
            if ordered and len(rejected) + skipped == len(ordered):
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
    }
    portal, detail = find_portal(make_opener(), cfg, timeout)
    if portal:
        info["portal"] = portal["base"]
        online, online_detail, uid = query_online(portal, cfg, timeout)
        info["online"], info["online_detail"], info["uid"] = online, online_detail, uid
    else:
        info["online_detail"] = detail
    ok, conn_detail = connectivity_ok(cfg, timeout)
    info["connectivity"], info["connectivity_detail"] = ok, conn_detail
    return info


# ============================================================================
# 首次配置向导
# ============================================================================

CARRIERS = {"1": ("@cmcc", "中国移动"), "2": ("@dx", "中国电信"), "3": ("@lt", "中国联通")}


def first_run_wizard(force=False):
    """没配置时问三个问题，写出 config.json。"""
    if not force and not (sys.stdin and sys.stdout and sys.stdout.isatty()):
        return False
    print()
    print("=" * 60)
    print(" 第一次运行，回答三个问题就能用了（回车 = 用方括号里的默认值）")
    print("=" * 60)
    try:
        account = ""
        while not account:
            account = input(" 1/3 一卡通号：").strip()
        password = input(" 2/3 密码（校园网和宽带是同一个）：").strip()
        print(" 3/3 宿舍的运营商宽带（SEU-ISP）是哪一家？")
        print("      1 = 中国移动   2 = 中国电信   3 = 中国联通")
        print("      回车 = 不确定 / 都写上，让程序自己去试")
        choice = input("      [回车 = 不确定，三家都配上]：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n 已取消。")
        return False

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["account"] = account
    cfg["password"] = password
    wlan_profile = cfg["profiles"][1]

    if choice in CARRIERS:
        # 明确知道是哪家：只配这一条 + 校园网
        suffix, carrier = CARRIERS[choice]
        isp_profiles = [{
            "name": "运营商宽带（宿舍，%s）" % carrier,
            "ssids": ["SEU-ISP"],
            "account_prefix": "",
            "account_suffix": suffix,
            "priority": 10,
            "note": "宿舍里的 SEU-ISP，走%s认证" % carrier,
        }]
    else:
        # 不确定：三家都写上，认证时会依次尝试，失败的那几条自动跳过。
        # 不要默认成 @cmcc——电信/联通用 @cmcc 会一直认证失败。
        isp_profiles = [{
            "name": "运营商宽带（宿舍，%s，不确定就都试）" % carrier,
            "ssids": ["SEU-ISP"],
            "account_prefix": "",
            "account_suffix": suffix,
            "priority": 10 + offset,
            "note": "不确定运营商时自动尝试；确认后可删掉用不上的两条",
        } for offset, (suffix, carrier) in enumerate(
            (CARRIERS["1"], CARRIERS["2"], CARRIERS["3"]))]

    wlan_profile["priority"] = 50
    cfg["profiles"] = isp_profiles + [wlan_profile]
    try:
        save_config(cfg)
    except OSError as exc:
        print(" 写配置失败：%s" % exc)
        return False
    print("\n 已保存：%s" % CONFIG_PATH)
    print(" 共写了 %d 条账号写法（宿舍走运营商宽带、教室走校园网），"
          "程序会自己挑能用的那条。" % len(cfg["profiles"]))
    print(" 不确定运营商的话，用不上的那几条会自动跳过，不用管。")
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

    button_login = make_button(buttons, "立即认证", lambda: run_action("login"))
    button_login.pack(side="left", padx=(0, 8))
    button_logout = make_button(buttons, "注销下线", lambda: run_action("logout"))
    button_logout.pack(side="left", padx=(0, 8))
    button_refresh = make_button(buttons, "刷新状态", lambda: refresh())
    button_refresh.pack(side="left", padx=(0, 8))
    button_config = make_button(buttons, "配置文件", open_config)
    button_config.pack(side="left", padx=(0, 8))
    button_log = make_button(buttons, "打开日志", open_log)
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
        info_label.configure(text="\n".join(lines))

        lines_tail = log.tail(200)
        tail.configure(state="normal")
        tail.delete("1.0", "end")
        tail.insert("end", "\n".join(lines_tail) if lines_tail else "（还没有日志）")
        tail.see("end")
        tail.configure(state="disabled")

    def set_busy(busy, text=""):
        state["busy"] = busy
        for button in (button_login, button_logout, button_refresh, button_config, button_log):
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
        root.after(300, lambda: messagebox.showinfo(
            "还差账号密码",
            "还没有配置一卡通号和密码。\n\n点「配置文件」填好 account / password 后，再点「立即认证」。"))

    root.mainloop()
    return 0


# ============================================================================
# 开机自启：launchd（macOS）/ 任务计划（Windows）
# ============================================================================

LAUNCHD_LABEL = "cn.edu.seu.net.autologin"


def _launchd_plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL)


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


def install_autostart(interval=60, log=print):
    launcher = _autostart_command()
    if IS_MAC:
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
  <key>StartInterval</key><integer>{interval}</integer>
  <key>StandardOutPath</key><string>{out}</string>
  <key>StandardErrorPath</key><string>{err}</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
""".format(
            label=LAUNCHD_LABEL,
            args="\n".join("    <string>%s</string>" % part
                           for part in launcher + ["login", "--quiet"]),
            interval=int(interval),
            out=os.path.join(BASE_DIR, "launchd.out.log"),
            err=os.path.join(BASE_DIR, "launchd.err.log"))
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
        log("每 %d 秒检查一次；日志在 %s" % (int(interval), os.path.join(BASE_DIR, "login.log")))
        log("要停用跑 `%s uninstall`" % os.path.basename(launcher[-1]))
        return 0

    # Windows：登录后触发 + 每 N 分钟
    task = "SEU-Net-AutoLogin"
    run(["schtasks", "/Delete", "/TN", task, "/F"])
    cmd = "%s login --quiet" % " ".join('"%s"' % part for part in launcher)
    code, out, err = run_result(
        ["schtasks", "/Create", "/TN", task, "/TR", cmd, "/SC", "MINUTE",
         "/MO", str(int(interval)), "/F", "/RL", "LIMITED"], encoding="gbk")
    if code != 0:
        log("创建计划任务 %s 失败（退出码 %d）：%s" % (task, code, (err or out).strip()[:200]))
        return 1
    log("已创建计划任务 %s（每 %d 分钟）" % (task, int(interval)))
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

    if command == "status":
        info = collect_status(cfg)
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
