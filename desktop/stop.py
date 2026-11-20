# -*- coding: utf-8 -*-
"""闲鱼智控 Pro —— 彻底停止

程序平时是常驻后台的（关窗口不停服），这个脚本用来真的把它关掉：
依次清掉 应用窗口 → 后台服务 → 常驻启动器（托盘图标随之消失）。

加 --autostart-off 会同时取消开机自启，下次开机就不会自己起来。
"""

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common  # noqa: E402

PROFILE_DIR = os.path.join(common.DESKTOP_DIR, "browser-profile")


def ps(script, timeout=30):
    return common.powershell(script, timeout=timeout)


def taskkill(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=common.CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def read_port():
    port = 8080
    try:
        with open(os.path.join(common.APP_DIR, "global_config.yml"), encoding="utf-8") as f:
            text = f.read()
        block = re.search(r"^AUTO_REPLY:.*?(?=^\S)", text, re.S | re.M)
        if block:
            m = re.search(r"^\s+port:\s*(\d+)", block.group(0), re.M)
            if m:
                port = int(m.group(1))
    except Exception:
        pass
    return port


def main():
    killed = []

    # 1) 管理界面窗口（用本程序专属浏览器配置的那些进程）
    script = (
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and ($_.CommandLine -like '*%s*') } | "
        "ForEach-Object { $_.ProcessId }" % PROFILE_DIR
    )
    for token in ps(script).replace("\r", " ").split():
        if token.isdigit() and taskkill(token):
            killed.append("管理界面窗口(PID %s)" % token)

    # 2) 后台服务：端口占用者（只认 python 进程，避免误杀）
    script = (
        "$c = Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; "
        "if ($c) { $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
        "if ($p -and $p.ProcessName -like 'python*') { $p.Id } }" % read_port()
    )
    out = ps(script)
    if out.isdigit() and taskkill(out):
        killed.append("后台服务(PID %s)" % out)

    # 3) 常驻启动器 / 残留 PID
    try:
        with open(common.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        for key, label in (("server", "后台服务"), ("launcher", "常驻启动器")):
            pid = state.get(key)
            if pid and taskkill(pid):
                killed.append("%s(PID %s)" % (label, pid))
    except Exception:
        pass

    # 4) 兜底：还在跑 Start.py 的 python 进程
    script = (
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*Start.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    for token in ps(script).replace("\r", " ").split():
        if token.isdigit() and taskkill(token):
            killed.append("后台服务残留(PID %s)" % token)

    try:
        if os.path.isfile(common.STATE_FILE):
            os.remove(common.STATE_FILE)
    except Exception:
        pass

    if killed:
        print("已停止：")
        for item in dict.fromkeys(killed):
            print("  - " + item)
    else:
        print("没有发现正在运行的闲鱼智控 Pro（可能已经关了）。")

    # 5) 开机自启：只在真的设过的时候才问，默认是关的
    auto_on = common.autostart_enabled()
    if auto_on or "--autostart-off" in sys.argv:
        turn_off = "--autostart-off" in sys.argv
        if not turn_off:
            try:
                ans = input("当前设置了开机自启，是否同时取消？[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            turn_off = ans in ("y", "yes", "是", "取消")
        if turn_off:
            if common.set_autostart(False):
                print("已取消开机自启，下次开机不会再自动启动。")
            else:
                print("[警告] 取消开机自启失败，可在托盘右键菜单里手动取消。")
        else:
            print("已保留开机自启设置，下次开机仍会自动运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
