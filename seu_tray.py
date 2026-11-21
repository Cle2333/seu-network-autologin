#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""菜单栏托盘（只在 macOS 上可用，需要 pyobjc）。

单独一个文件，是为了让主程序 seu_net.py 保持「纯标准库、零依赖」——
没装 pyobjc 也完全不影响认证、命令行和图形界面。

用法：
    python3 seu_tray.py            # 常驻菜单栏
    python3 seu_tray.py --install  # 装成开机自启（用托盘启动）

托盘显示当前状态，点开可以立即认证、注销、打开界面。
和 launchd 里那个 `login --quiet` 任务共存：两边都受单实例锁保护，
谁先拿到谁做，不会互相踢会话。
"""

import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seu_net as core  # noqa: E402

# 托盘依赖装在项目目录自己的 venv 里（不是 /tmp，重启不会丢）
VENV_DIR = os.path.join(core.BASE_DIR, ".venv-tray")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python")


def _in_tray_venv():
    try:
        return os.path.samefile(sys.executable, VENV_PYTHON)
    except OSError:
        return False


try:
    import objc
    from AppKit import (
        NSApplication, NSApplicationActivationPolicyAccessory, NSImage,
        NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength,
    )
    from Foundation import NSTimer, NSObject
    HAS_PYOBJC = True
except ImportError:
    HAS_PYOBJC = False


def _missing_dependency_message():
    print("托盘功能需要 pyobjc，但当前 Python 里没有：")
    print("  %s" % sys.executable)
    print()
    if os.path.exists(VENV_PYTHON):
        print("项目里已经有装好的环境，直接用它启动：")
        print("  %s seu_tray.py" % VENV_PYTHON)
    else:
        print("自动装一个（会建 %s 并下载 pyobjc，约 20 MB）：" % VENV_DIR)
        print("  python3 seu_tray.py --setup")
    print()
    print("不装托盘也完全不影响认证，命令行和图形界面照常可用：")
    print("  python3 seu_net.py status    看状态")
    print("  python3 seu_net.py           认证")
    print("  python3 seu_net.py gui       图形界面")


# 状态用系统自带的 SF Symbol（不打包图片资源），文字跟在图标后面
STATE_SYMBOLS = {
    "online": ("checkmark.circle.fill", "已联网"),
    "offline": ("exclamationmark.circle.fill", "未认证"),
    "working": ("arrow.triangle.2.circlepath", "认证中"),
    "unknown": ("questionmark.circle", "不在校园网"),
}


# 没装 pyobjc 时后面的类定义会直接 NameError，所以给个占位基类：
# 这样 --setup 和友好提示都能正常走到，而不是抛一堆 traceback。
# 这些占位对象在真正用到之前就会因为 HAS_PYOBJC 检查而返回，不会被调用。
if not HAS_PYOBJC:
    class NSObject(object):          # noqa: N801
        pass

    class _MissingObjc(object):
        @staticmethod
        def super(*_args):
            return object

    objc = _MissingObjc()

    def _placeholder(*_args, **_kwargs):
        raise RuntimeError("pyobjc 未安装")

    NSImage = NSMenu = NSMenuItem = NSStatusBar = NSTimer = NSApplication = _placeholder
    NSVariableStatusItemLength = 0
    NSApplicationActivationPolicyAccessory = 0


class TrayController(NSObject):
    """把 pyobjc 的回调转成对 core 的调用。"""

    def initWithHelper_(self, helper):
        self = objc.super(TrayController, self).init()
        if self is None:
            return None
        self.helper = helper
        return self

    # ---- 菜单动作 ----
    def doLogin_(self, _sender):
        self.helper.run_async("login")

    def doLogout_(self, _sender):
        self.helper.run_async("logout")

    def doOpenGui_(self, _sender):
        self.helper.open_gui()

    def doOpenLog_(self, _sender):
        self.helper.open_log()

    def doRefresh_(self, _sender):
        self.helper.refresh_now()

    def doQuit_(self, _sender):
        NSApplication.sharedApplication().terminate_(None)

    # ---- 从后台线程回到主线程（TrayHelper 不是 NSObject，不能自己调度）----
    def applyStatus_(self, info):
        self.helper.apply_status(info)

    def afterAction_(self, message):
        self.helper.after_action(message)


class TrayHelper(object):
    """托盘的全部逻辑：状态刷新、菜单、后台任务。"""

    REFRESH_SECONDS = 30

    def __init__(self):
        self.cfg = core.load_config()
        self.log = core.Logger(self.cfg, verbose=False)
        self.status = None
        self.busy = False
        self.item = None
        self.controller = None

    # ---- 界面构建 ----
    def build(self):
        self.controller = TrayController.alloc().initWithHelper_(self)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        self.item.button().setEnabled_(True)
        # 图标 + 文字都放上去：只放文字时有些系统版本会把按钮画成空白
        self._apply("wifi", "校园网")

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        self.status_item = self._add(menu, "正在检查…", None)
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "立即认证", "doLogin:")
        self._add(menu, "注销下线", "doLogout:")
        self._add(menu, "刷新状态", "doRefresh:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "打开图形界面", "doOpenGui:")
        self._add(menu, "打开日志", "doOpenLog:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "退出托盘", "doQuit:")

        self.item.setMenu_(menu)

    def _add(self, menu, title, selector):
        if selector is None:
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, "")
            entry.setEnabled_(False)
        else:
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, selector, "")
            entry.setTarget_(self.controller)
            entry.setEnabled_(True)
        menu.addItem_(entry)
        return entry

    # ---- 状态刷新 ----
    def refresh_now(self):
        if self.busy:
            return
        self.busy = True
        self._apply("arrow.triangle.2.circlepath", "检查中")
        self.status_item.setTitle_("正在检查…")
        # 网络请求不能阻塞主线程（否则菜单栏会卡住转圈）
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            cfg = core.load_config()
            info = core.collect_status(cfg)
        except Exception as exc:
            info = {"error": "%s: %s" % (exc.__class__.__name__, exc)}
        # 回到主线程更新界面：TrayHelper 不是 NSObject，借 controller 调度
        self.controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"applyStatus:", info, False)

    def apply_status(self, info):
        self.busy = False
        self.status = info
        if "error" in info:
            self._apply("exclamationmark.triangle.fill", "出错")
            self.status_item.setTitle_("检查失败：%s" % info["error"][:40])
            return
        if info["connectivity"]:
            key, detail = "online", "已联网"
        elif info["online"]:
            key, detail = "offline", "门户在线但外网不通"
        elif info["portal"]:
            key, detail = "offline", "已连校园网，未认证"
        else:
            key, detail = "unknown", "不在校园网"
        symbol, short = STATE_SYMBOLS[key]
        self._apply(symbol, short)
        if info.get("uid"):
            detail += "（%s）" % info["uid"]
        self.status_item.setTitle_(detail)

    def _apply(self, symbol_name, text):
        """把图标和文字装到菜单栏按钮上。

        顺序很重要：必须先 setTitle_ 再 setImage_，最后 setImagePosition_。
        反过来做的话 imagePosition 会被重置成 7（NSImageOnly），文字就不显示了。
        用系统自带 SF Symbol 当图标，省掉打包图片资源。
        """
        button = self.item.button()
        button.setTitle_(text[:16])
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            symbol_name, text)
        if image is not None:
            button.setImage_(image)
            button.setImagePosition_(2)   # NSImageLeft：图标在左，文字在右

    # ---- 后台动作 ----
    def run_async(self, action):
        if self.busy:
            self.status_item.setTitle_("上一个操作还没结束")
            return
        self.busy = True
        self._apply("arrow.triangle.2.circlepath",
                    "认证中" if action == "login" else "注销中")
        threading.Thread(target=self._action_worker, args=(action,),
                         daemon=True).start()

    def _action_worker(self, action):
        cfg = core.load_config()
        log = core.Logger(cfg, verbose=False)
        message = ""
        try:
            if action == "login":
                code = core.authenticate(cfg, log, once=False)
                message = {0: "认证完成", 1: "认证超时", 2: "账号被拒",
                           3: "配置不完整"}.get(code, "认证结束（%s）" % code)
            else:
                portal, _detail = core.find_portal(
                    core.make_opener(), cfg,
                    float(cfg.get("request_timeout_seconds", 4)))
                if portal:
                    ok, _text = core.do_logout(portal, cfg, log)
                    message = "已注销" if ok else "门户拒绝注销"
                else:
                    message = "找不到门户"
        except Exception as exc:
            message = "出错：%s" % exc.__class__.__name__
        self.controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"afterAction:", message, False)

    def after_action(self, message):
        self.busy = False
        self.status_item.setTitle_(message)
        # 稍等一下再刷新，让用户看清结果
        self.refresh_now()

    # ---- 外部程序 ----
    def open_gui(self):
        script = os.path.join(core.BASE_DIR, "seu_net.py")
        subprocess.Popen([sys.executable, script, "gui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_log(self):
        path = core.Logger(core.load_config(), verbose=False).path
        if os.path.exists(path):
            subprocess.Popen(["open", path])
        else:
            self.status_item.setTitle_("还没有日志")


def run():
    if sys.platform != "darwin":
        print("托盘只在 macOS 上提供。Windows 请用 `seu_net.py gui`。")
        return 1

    app = NSApplication.sharedApplication()
    # .accessory = 不占 Dock、不抢焦点，就是纯菜单栏程序
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    helper = TrayHelper()
    helper.build()
    helper.refresh_now()

    # 定时刷新（NSTimer 挂在主 run loop 上，不会阻塞菜单）
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        helper.REFRESH_SECONDS, helper.controller, b"doRefresh:", None, True)
    app.setDelegate_(helper.controller)

    app.run()
    return 0


def install_dependencies():
    """在项目目录建 venv 并装 pyobjc，装完就能常驻托盘。

    装在项目里而不是 /tmp，重启也不会丢；也不用污染系统 Python。
    """
    if sys.platform != "darwin":
        print("托盘只在 macOS 上提供。")
        return 1
    print("建立环境：%s" % VENV_DIR)
    code, out, err = core.run_result([sys.executable, "-m", "venv", VENV_DIR])
    if code != 0:
        print("建 venv 失败：%s" % (err or out).strip()[:200])
        return 1
    print("安装 pyobjc（约 20 MB，需要联网）…")
    code, out, err = core.run_result(
        [VENV_PYTHON, "-m", "pip", "install", "--quiet",
         "pyobjc-framework-Cocoa"], timeout=300)
    if code != 0:
        print("安装失败：%s" % (err or out).strip()[:300])
        print("可以手动装：%s -m pip install pyobjc-framework-Cocoa" % VENV_PYTHON)
        return 1
    code, _out, _err = core.run_result([VENV_PYTHON, "-c", "import AppKit"])
    if code != 0:
        print("装完了但 import AppKit 仍然失败，可能是 pyobjc 版本和 Python %s 不匹配。"
              % ".".join(map(str, sys.version_info[:2])))
        return 1
    print()
    print("装好了。现在可以：")
    print("  %s seu_tray.py             # 直接跑托盘" % VENV_PYTHON)
    print("  %s seu_tray.py --install   # 装成开机自启（开机就常驻菜单栏）"
          % VENV_PYTHON)
    return 0


def main(argv):
    if "--setup" in argv:
        return install_dependencies()
    if not HAS_PYOBJC:
        _missing_dependency_message()
        return 1
    if sys.platform != "darwin":
        print("托盘只在 macOS 上提供。Windows 请用 `seu_net.py gui`。")
        return 1
    if "--install" in argv:
        return core.install_autostart(tray=True)
    if "--uninstall" in argv:
        return core.uninstall_autostart()
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
