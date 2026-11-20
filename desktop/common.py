# -*- coding: utf-8 -*-
"""闲鱼智控 Pro —— 桌面层公共工具

launcher.pyw / bootstrap.py / stop.py 共用：路径常量、日志、PowerShell 调用、
桌面快捷方式与开机自启快捷方式的创建/删除。
"""

import os
import subprocess
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP_DIR = os.path.join(APP_DIR, "desktop")
RUNTIME_DIR = os.path.join(APP_DIR, "runtime", "python")
PYTHON = os.path.join(RUNTIME_DIR, "python.exe")
PYTHONW = os.path.join(RUNTIME_DIR, "pythonw.exe")
BROWSERS_DIR = os.path.join(APP_DIR, "runtime", "ms-playwright")
LAUNCHER = os.path.join(DESKTOP_DIR, "launcher.pyw")
ICON_PATH = os.path.join(DESKTOP_DIR, "app.ico")
STATE_FILE = os.path.join(DESKTOP_DIR, "state.json")
READY_MARK = os.path.join(DESKTOP_DIR, ".ready")
SERVER_LOG = os.path.join(DESKTOP_DIR, "server.log")
LAUNCH_LOG = os.path.join(DESKTOP_DIR, "desktop.log")
BOOTSTRAP_LOG = os.path.join(DESKTOP_DIR, "bootstrap.log")

APP_TITLE = "闲鱼智控 Pro"
SHORTCUT_NAME = "闲鱼智控 Pro"

CREATE_NO_WINDOW = 0x08000000


def ensure_dirs():
    if not os.path.isdir(DESKTOP_DIR):
        os.makedirs(DESKTOP_DIR)


def log(msg, path=None):
    try:
        ensure_dirs()
        with open(path or LAUNCH_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass


def powershell(script, timeout=60):
    """静默跑一段 PowerShell，返回 stdout（失败返回空串）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def shell_folder(name):
    """取系统文件夹真实路径（兼容 OneDrive 重定向）：Desktop / Startup。"""
    path = powershell("[Environment]::GetFolderPath('%s')" % name, timeout=25)
    if path and os.path.isdir(path):
        return path
    if name == "Desktop":
        return os.path.join(os.path.expanduser("~"), "Desktop")
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _make_lnk(lnk_path, arguments, description):
    target = PYTHONW
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut('%s'); "
        "$sc.TargetPath = '%s'; "
        "$sc.Arguments = '%s'; "
        "$sc.WorkingDirectory = '%s'; "
        "$sc.IconLocation = '%s'; "
        "$sc.Description = '%s'; "
        "$sc.Save()"
    ) % (lnk_path, target, arguments, APP_DIR, ICON_PATH, description)
    powershell(script)
    return os.path.isfile(lnk_path)


def desktop_shortcut_path():
    return os.path.join(shell_folder("Desktop"), SHORTCUT_NAME + ".lnk")


def create_desktop_shortcut():
    """桌面图标：双击 = 打开界面（程序已在后台时会直接把窗口叫出来）。"""
    lnk = desktop_shortcut_path()
    args = '"%s"' % LAUNCHER
    ok = _make_lnk(lnk, args, "闲鱼智控 Pro - 打开管理界面")
    return ok, lnk


def autostart_shortcut_path():
    return os.path.join(shell_folder("Startup"), SHORTCUT_NAME + ".lnk")


def create_autostart_shortcut():
    """开机自启：登录后静默拉起后台服务（不弹窗口，只在托盘显示图标）。"""
    lnk = autostart_shortcut_path()
    args = '"%s" --silent' % LAUNCHER
    ok = _make_lnk(lnk, args, "闲鱼智控 Pro - 开机后台启动")
    return ok, lnk


def autostart_enabled():
    return os.path.isfile(autostart_shortcut_path())


def shortcut_valid(lnk_path, expect_extra=""):
    """快捷方式存在、并且指向当前这份安装（文件夹被挪动/改名后自动失效）。"""
    if not os.path.isfile(lnk_path):
        return False
    if not os.path.isfile(PYTHONW):
        return False
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut('%s'); "
        "$sc.TargetPath + '|' + $sc.Arguments" % lnk_path
    )
    out = powershell(script, timeout=25).lower()
    if not out:
        return False
    app = APP_DIR.lower()
    if app not in out:
        return False
    if expect_extra and expect_extra.lower() not in out:
        return False
    return os.path.basename(PYTHONW).lower() in out


def set_autostart(enabled):
    if enabled:
        ok, _ = create_autostart_shortcut()
        return ok
    try:
        lnk = autostart_shortcut_path()
        if os.path.isfile(lnk):
            os.remove(lnk)
        return not os.path.isfile(lnk)
    except Exception:
        return False
