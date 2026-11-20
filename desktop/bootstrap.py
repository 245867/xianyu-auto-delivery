# -*- coding: utf-8 -*-
"""闲鱼智控 Pro —— 运行环境自检 / 修复

可以随时重复运行，已经准备好的项目会自动跳过：

  1. 检查内置 Python 运行环境（runtime\\python\\python.exe）
  2. 检查第三方依赖，缺哪个就补装哪个
  3. 检查 Playwright 浏览器内核，缺了就补下载
  4. 建好桌面快捷方式（开机自启需要显式加 --autostart）

用法：
  python.exe desktop\\bootstrap.py                 完整自检（首次运行用）
  python.exe desktop\\bootstrap.py --force         强制重新自检
  python.exe desktop\\bootstrap.py --shortcut      只重建桌面快捷方式
  python.exe desktop\\bootstrap.py --autostart     顺便设置开机自启（默认不设）
"""

import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common  # noqa: E402

# (导入名, pip 包名) —— 与源码里实际 import 的第三方模块对齐
REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("starlette", "starlette"),
    ("pydantic", "pydantic"),
    ("aiohttp", "aiohttp"),
    ("websockets", "websockets"),
    ("loguru", "loguru"),
    ("yaml", "PyYAML"),
    ("requests", "requests"),
    ("httpx", "httpx"),
    ("psutil", "psutil"),
    ("openai", "openai"),
    ("PIL", "Pillow"),
    ("qrcode", "qrcode"),
    ("playwright", "playwright"),
    ("DrissionPage", "DrissionPage"),
    ("blackboxprotobuf", "blackboxprotobuf"),
    ("execjs", "PyExecJS"),
    ("passlib", "passlib"),
    ("pandas", "pandas"),
    ("openpyxl", "openpyxl"),
    ("pystray", "pystray"),        # 托盘图标（后台常驻的关键）
    ("six", "six"),
]


def say(msg=""):
    text = str(msg)
    print(text, flush=True)
    try:
        with open(common.BOOTSTRAP_LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def env_with_browsers():
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = common.BROWSERS_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def missing_modules():
    mods = [name for name, _ in REQUIRED]
    code = (
        "import importlib\n"
        "bad = []\n"
        "for m in %r:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        bad.append(m)\n"
        "print('|'.join(bad))\n"
    ) % (mods,)
    try:
        out = subprocess.run([common.PYTHON, "-c", code], capture_output=True, text=True,
                             timeout=180, env=env_with_browsers(),
                             creationflags=common.CREATE_NO_WINDOW)
    except Exception as exc:
        say("  [警告] 依赖自检没跑起来：%r" % (exc,))
        return []
    return [x for x in (out.stdout or "").strip().split("|") if x]


def chromium_ok():
    if not os.path.isdir(common.BROWSERS_DIR):
        return False
    for name in sorted(os.listdir(common.BROWSERS_DIR)):
        if not name.lower().startswith("chromium"):
            continue
        for layout in ("chrome-win64", "chrome-win"):
            exe = os.path.join(common.BROWSERS_DIR, name, layout, "chrome.exe")
            if os.path.isfile(exe) and os.path.getsize(exe) > 0:
                return True
    return False


def pip_install(pkg):
    say("  正在安装：%s" % pkg)
    try:
        rc = subprocess.call(
            [common.PYTHON, "-m", "pip", "install", "--no-input",
             "--disable-pip-version-check", pkg],
            cwd=common.APP_DIR, env=env_with_browsers(),
        )
        return rc == 0
    except Exception as exc:
        say("  [失败] %s：%r" % (pkg, exc))
        return False


def install_browsers():
    say("  正在下载 Playwright 浏览器内核（约 330MB，视网速 1-10 分钟）…")
    rc = subprocess.call([common.PYTHON, "-m", "playwright", "install", "chromium"],
                         cwd=common.APP_DIR, env=env_with_browsers())
    if rc != 0:
        say("  [失败] playwright install chromium 返回 %s" % rc)
        return False
    return chromium_ok()


def write_ready():
    try:
        import json
        info = {
            "ready_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "chromium": chromium_ok(),
            "pystray": True,
        }
        with open(common.READY_MARK, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def make_shortcuts(with_autostart, force=False):
    lnk = common.desktop_shortcut_path()
    if not force and common.shortcut_valid(lnk):
        say("  桌面快捷方式：已就绪")
    else:
        ok, lnk = common.create_desktop_shortcut()
        say("  桌面快捷方式：%s  %s" % ("已就绪" if ok else "创建失败", lnk))

    lnk2 = common.autostart_shortcut_path()
    if not with_autostart:
        return
    if not force and common.shortcut_valid(lnk2, "--silent"):
        say("  开机自启：已设置")
    else:
        ok2, lnk2 = common.create_autostart_shortcut()
        say("  开机自启：%s  %s" % ("已设置" if ok2 else "设置失败", lnk2))


def main():
    only_shortcut = "--shortcut" in sys.argv
    force = "--force" in sys.argv
    # 开机自启默认不设：用户需要时在托盘右键勾选「开机自启」，或加 --autostart 重跑一次
    with_autostart = "--autostart" in sys.argv

    common.ensure_dirs()
    try:
        with open(common.BOOTSTRAP_LOG, "w", encoding="utf-8") as f:
            f.write("===== 自检 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass

    print("=" * 58)
    print("  闲鱼智控 Pro · 运行环境自检")
    print("=" * 58)

    if only_shortcut:
        make_shortcuts(with_autostart, force=True)
        print("\n完成。以后双击桌面上的「%s」即可。" % common.SHORTCUT_NAME)
        return 0



    if not os.path.isfile(common.PYTHON):
        print("[错误] 找不到内置运行环境：%s" % common.PYTHON)
        print("       请确认整个文件夹完整，没有被杀毒软件隔离。")
        return 1

    fast_path = os.path.isfile(common.READY_MARK) and not force
    if fast_path:
        print("[1/3] 运行环境：已就绪，跳过")
        print("[2/3] 依赖与浏览器内核：已就绪，跳过")
    else:
        print("[1/3] 检查第三方依赖…")
        say("步骤1 依赖检查")
        bad = missing_modules()
        if not bad:
            say("  依赖齐全")
        else:
            say("  缺少：%s" % ", ".join(bad))
            mapping = dict(REQUIRED)
            still = []
            for mod in bad:
                if not pip_install(mapping.get(mod, mod)):
                    still.append(mapping.get(mod, mod))
            if still:
                say("[错误] 补装失败：%s" % ", ".join(still))
                say("       常见原因：没有联网，或被杀毒软件拦截。")
                return 1
            left = missing_modules()
            if left:
                say("[错误] 装完仍然导入失败：%s" % ", ".join(left))
                return 1
            say("  依赖修补完成")

        print("[2/3] 检查 Playwright 浏览器内核…")
        say("步骤2 浏览器内核检查")
        if chromium_ok():
            say("  内核已就绪")
        elif not install_browsers():
            say("[警告] 浏览器内核没装好。程序仍可启动，但扫码登录/订单抓取会失败。")
            say("       稍后可重新双击「启动闲鱼智控Pro.bat」重试。")

    print("[3/3] 桌面快捷方式…")
    make_shortcuts(with_autostart)

    write_ready()
    print("")
    print("自检完成，正在启动。程序会常驻在任务栏右侧（托盘图标），")
    print("关掉窗口不会停止自动发货；彻底退出用托盘右键「退出」或「强制停止.bat」。")
    print("（开机自启未设置：需要时在托盘右键勾选「开机自启」即可。）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        detail = traceback.format_exc()
        print(detail)
        try:
            with open(common.BOOTSTRAP_LOG, "a", encoding="utf-8") as f:
                f.write(detail)
        except Exception:
            pass
        sys.exit(1)
