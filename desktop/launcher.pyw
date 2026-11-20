# -*- coding: utf-8 -*-
"""闲鱼智控 Pro —— 后台常驻启动器

运行形态（和正常桌面软件一样）：

  * 双击图标 / 开机自启 → 后台静默起服务，任务栏右侧出现「闲鱼智控 Pro」托盘图标
  * 管理界面是一个独立窗口（Edge/Chrome 应用模式），**关掉窗口只是关界面**
  * 关窗口后服务继续跑，自动发货、消息监听、商品同步都不受影响
  * 托盘菜单：打开管理界面 / 打开日志文件夹 / 开机自启（可勾选）/ 退出
  * 服务进程意外退出会自动拉起（连续失败 5 次才放弃并提示）
  * 想彻底退出：托盘右键「退出」，或双击「强制停止.bat」

日志：desktop\\desktop.log（启动器）、desktop\\server.log（后台服务）
"""

import ctypes
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common  # noqa: E402

PROFILE_DIR = os.path.join(common.DESKTOP_DIR, "browser-profile")
STARTUP_TIMEOUT = 300          # 等后台服务就绪的最长秒数
WATCH_INTERVAL = 6             # 守护检查间隔（秒）
MAX_RESTARTS = 5               # 连续自动拉起次数上限
MUTEX_NAME = "XianyuZhikongPro_Owner"
MB_ICONINFO = 0x40
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30

_QUIT = threading.Event()
_LOCK = threading.Lock()
_SERVER = {"proc": None, "restarts": 0, "started_at": 0.0}
_ICON = {"obj": None}


# ---------------------------------------------------------------- 基础工具

def log(msg):
    common.log(msg)


def msgbox(text, title=common.APP_TITLE, icon=MB_ICONINFO):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, icon)
    except Exception:
        pass


def notify(text, title=common.APP_TITLE):
    icon = _ICON.get("obj")
    if icon is None:
        return False
    try:
        icon.notify(text, title)
        return True
    except Exception:
        return False


def read_host_port():
    """从 global_config.yml 读 AUTO_REPLY.api 的 host/port，读不到就用默认值。"""
    host, port = "127.0.0.1", 8080
    path = os.path.join(common.APP_DIR, "global_config.yml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"^AUTO_REPLY:.*?(?=^\S)", text, re.S | re.M)
        if block:
            piece = block.group(0)
            m = re.search(r"^\s+port:\s*(\d+)", piece, re.M)
            if m:
                port = int(m.group(1))
            m = re.search(r"^\s+host:\s*([0-9A-Za-z_.\-:]+)", piece, re.M)
            if m:
                h = m.group(1)
                host = "127.0.0.1" if h in ("0.0.0.0", "::", "*") else h
    except Exception as exc:
        log("读取配置失败，改用默认端口：%r" % (exc,))
    return host, port


def port_open(host, port, timeout=0.8):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_state():
    try:
        with open(common.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data):
    try:
        with open(common.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def clear_state():
    try:
        if os.path.isfile(common.STATE_FILE):
            os.remove(common.STATE_FILE)
    except Exception:
        pass


def tail_text(path, lines=40):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 64 * 1024))
            raw = f.read()
        text = raw.decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return "（读不到日志文件）"


# ---------------------------------------------------------------- 界面窗口

def find_browser():
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def launch_window(url):
    """独立窗口：应用模式 + 专属配置目录，和日常浏览器互不干扰。
    同一个配置目录 + 同一个地址，重复打开只会把已有窗口叫到前台。"""
    browser = find_browser()
    if not browser:
        log("没找到 Edge/Chrome，改用默认浏览器")
        webbrowser.open(url)
        return None
    if not os.path.isdir(PROFILE_DIR):
        os.makedirs(PROFILE_DIR)
    args = [
        browser,
        "--app=" + url,
        "--user-data-dir=" + PROFILE_DIR,
        "--window-size=1480,940",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
    ]
    log("打开管理界面")
    try:
        return subprocess.Popen(args, cwd=common.DESKTOP_DIR,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as exc:
        log("窗口启动失败：%r" % (exc,))
        webbrowser.open(url)
        return None


# ---------------------------------------------------------------- 后台服务

def start_server(port):
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = common.BROWSERS_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    common.ensure_dirs()
    with open(common.SERVER_LOG, "a", encoding="utf-8", errors="replace") as logf:
        logf.write("\n\n===== 启动后台服务 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        logf.flush()
        log("启动后台服务，端口 %s" % port)
        return subprocess.Popen(
            [common.PYTHON, "Start.py"],
            cwd=common.APP_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=common.CREATE_NO_WINDOW,
        )


def health_ready(host, port):
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        payload = json.loads(response.read(8192))
        return isinstance(payload, dict) and payload.get("status") == "healthy"
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()


def wait_ready(host, port, proc):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not _QUIT.is_set() and time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            log("后台服务提前退出，返回码 %s" % proc.returncode)
            return False
        if health_ready(host, port):
            return proc is None or proc.poll() is None
        _QUIT.wait(0.5)
    if not _QUIT.is_set():
        log("等待健康检查通过超时（%s 秒）" % STARTUP_TIMEOUT)
    return False


def stop_server():
    with _LOCK:
        proc = _SERVER.get("proc")
        _SERVER["proc"] = None
    if proc is not None and proc.poll() is None:
        log("结束后台服务 PID %s" % proc.pid)
        try:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except Exception:
                proc.kill()
        except Exception as exc:
            log("结束服务失败：%r" % (exc,))
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=common.CREATE_NO_WINDOW)
        except Exception:
            pass


def stop_server_by_pid(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=common.CREATE_NO_WINDOW)
    except Exception:
        pass


def boot_server(host, port, quiet=False):
    try:
        with _LOCK:
            if _QUIT.is_set():
                return False
            if port_open(host, port):
                if health_ready(host, port):
                    log("端口 %s 已有健康的后台服务在运行，直接复用" % port)
                    _SERVER["proc"] = None
                    _SERVER["started_at"] = time.monotonic()
                    save_state({
                        "launcher": os.getpid(),
                        "server": None,
                        "url": "http://%s:%d/" % (host, port),
                        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    return True
                raise OSError("端口 %s 已被占用" % port)
            proc = start_server(port)
            _SERVER["proc"] = proc
        if wait_ready(host, port, proc):
            with _LOCK:
                if not _QUIT.is_set() and _SERVER["proc"] is proc and proc.poll() is None:
                    _SERVER["started_at"] = time.monotonic()
                    save_state({
                        "launcher": os.getpid(),
                        "server": proc.pid,
                        "url": "http://%s:%d/" % (host, port),
                        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    return True
    except OSError as exc:
        log("无法启动后台服务：%s" % exc)
    stop_server()
    clear_state()
    if not quiet and not _QUIT.is_set():
        detail = tail_text(common.SERVER_LOG, 40)
        msgbox("后台服务没能正常启动。\n\n最近的运行日志（%s）：\n\n%s"
               % (common.SERVER_LOG, detail), icon=MB_ICONERROR)
    return False


def watchdog(host, port):
    """服务进程万一挂了，自动拉起来——自动发货不能因为崩一次就断。"""
    while not _QUIT.wait(WATCH_INTERVAL):
        with _LOCK:
            proc = _SERVER.get("proc")
            restarts = _SERVER.get("restarts", 0)
            started_at = _SERVER.get("started_at", 0)
        if proc is not None and proc.poll() is None:
            # 稳定运行超过 10 分钟就把连续失败计数清零
            if started_at and time.monotonic() - started_at > 600 and restarts:
                with _LOCK:
                    _SERVER["restarts"] = 0
            continue
        if proc is None and health_ready(host, port):
            # 复用已有服务且健康运行中
            continue
        if restarts >= MAX_RESTARTS:
            log("已连续自动拉起 %s 次仍失败，不再重试" % restarts)
            notify("后台服务反复退出，已停止自动重启。请查看 desktop\\server.log")
            stop_server()
            clear_state()
            return
        with _LOCK:
            _SERVER["restarts"] = restarts + 1
            n = _SERVER["restarts"]
        if proc is not None:
            log("后台服务已退出（返回码 %s）" % proc.returncode)
        else:
            log("复用的后台服务已不可达")
        log("第 %s 次自动拉起后台服务" % n)
        if _QUIT.wait(3):
            return
        if boot_server(host, port, quiet=True):
            notify("后台服务已自动重启" if n == 1 else "后台服务第 %s 次自动重启" % n)


# ---------------------------------------------------------------- 托盘

def load_icon_image():
    try:
        from PIL import Image
        if os.path.isfile(common.ICON_PATH):
            return Image.open(common.ICON_PATH)
    except Exception:
        pass
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (255, 204, 0, 255))
        d = ImageDraw.Draw(img)
        d.ellipse((16, 24, 52, 44), fill=(32, 36, 46, 255))
        d.polygon([(20, 34), (4, 20), (4, 48)], fill=(32, 36, 46, 255))
        return img
    except Exception:
        log("图标生成失败（不影响运行）")
        return None


def run_tray(url):
    import pystray

    def on_open(icon, item):
        try:
            launch_window(url)
        except Exception:
            log("打开界面失败：\n" + traceback.format_exc())

    def on_logs(icon, item):
        try:
            os.startfile(common.DESKTOP_DIR)
        except Exception:
            log("打开日志文件夹失败：\n" + traceback.format_exc())

    def on_autostart(icon, item):
        try:
            common.set_autostart(not common.autostart_enabled())
            icon.update_menu()
        except Exception:
            log("切换开机自启失败：\n" + traceback.format_exc())

    def on_quit(icon, item):
        log("用户从托盘选择退出")
        _QUIT.set()
        try:
            icon.stop()
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("打开管理界面", on_open, default=True),
        pystray.MenuItem("打开日志文件夹", on_logs),
        pystray.MenuItem("开机自启", on_autostart,
                         checked=lambda item: common.autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出（停止自动发货）", on_quit),
    )
    icon = pystray.Icon("xianyu_zhikong_pro", load_icon_image(),
                        "闲鱼智控 Pro · 后台运行中", menu)
    _ICON["obj"] = icon

    def setup(icon_):
        notify("已在后台运行，关掉窗口不会停止自动发货")

    icon.run(setup=setup)


# ---------------------------------------------------------------- 自检

def ensure_ready():
    if os.path.isfile(common.READY_MARK):
        return True
    log("未检测到就绪标记，进入首次自检")
    try:
        rc = subprocess.call(
            [common.PYTHON, os.path.join(common.DESKTOP_DIR, "bootstrap.py")],
            cwd=common.APP_DIR, creationflags=0x00000010,  # CREATE_NEW_CONSOLE
        )
    except Exception as exc:
        log("自检脚本调用失败：%r" % (exc,))
        rc = -1
    if rc == 0 and os.path.isfile(common.READY_MARK):
        return True
    msgbox("运行环境自检没通过（返回码 %s）。\n\n%s\n\n完整日志：%s"
           % (rc, tail_text(common.BOOTSTRAP_LOG, 30), common.BOOTSTRAP_LOG),
           icon=MB_ICONERROR)
    return False


# ---------------------------------------------------------------- 主流程

def main():
    common.ensure_dirs()
    silent = "--silent" in sys.argv
    log("---- 启动器开始 silent=%s ----" % silent)

    if not os.path.isfile(common.PYTHON):
        msgbox("找不到内置运行环境：\n%s\n\n请确认整个文件夹完整（可能被杀毒软件隔离了 runtime 目录）。"
               % common.PYTHON, icon=MB_ICONERROR)
        return 1

    host, port = read_host_port()
    url = "http://%s:%d/" % (host, port)

    # 单实例：只允许一个"常驻主进程"
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already = ctypes.windll.kernel32.GetLastError() == 183
    except Exception:
        already = False

    if already:
        # 程序已在后台跑：这次双击只负责把界面窗口叫出来，然后自己退出
        log("检测到常驻实例，等待服务就绪后打开界面")
        if wait_ready(host, port, None):
            launch_window(url)
        else:
            log("常驻实例在端口 %s 的健康检查未通过" % port)
            return 1
        return 0

    # 自启时不弹窗口，只留托盘
    if not ensure_ready():
        return 1

    # 每日备份（失败不阻塞启动）：data\ 与 global_config.yml → 05-备份\，保留 7 份
    try:
        import backup as _backup
        _backup.run()
    except Exception:
        log("备份跳过：\n" + traceback.format_exc())

    if not boot_server(host, port):
        return 1

    threading.Thread(target=watchdog, args=(host, port), daemon=True).start()

    if not silent:
        launch_window(url)

    try:
        run_tray(url)
    except Exception:
        log("托盘图标不可用，降级为无界面常驻：\n" + traceback.format_exc())
        msgbox(
            "托盘图标不可用，程序仍在后台运行（自动发货不受影响）。\n\n"
            "打开界面：双击桌面「闲鱼智控 Pro」\n"
            "完全退出：双击「强制停止.bat」", icon=MB_ICONWARNING)
        while not _QUIT.is_set():
            time.sleep(5)

    _QUIT.set()
    stop_server()
    clear_state()
    log("---- 已退出 ----")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        detail = traceback.format_exc()
        log("未捕获异常：\n" + detail)
        msgbox("启动器出错：\n\n%s" % detail[-1500:], icon=MB_ICONERROR)
        sys.exit(1)
